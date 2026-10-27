"""Backups: public_html + MySQL dumps -> /var/backups/ypanel/<domain>/*.tar.gz"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import access, config, db, remote
from ..system import bad, run, run_raw

router = APIRouter(prefix="/api/backups", tags=["backups"])
STALE = 6 * 3600


def _site(site_id):
    access.assert_site(site_id)
    s = db.q1("SELECT * FROM sites WHERE id=?", (site_id,))
    if not s:
        raise HTTPException(404, "Site not found")
    return s


def _site_dir(domain):
    p = config.BACKUP_ROOT / domain
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(config.BACKUP_ROOT, 0o700)
    return p


def _backup(bid):
    b = db.q1("SELECT * FROM backups WHERE id=?", (bid,))
    if b and not access.can_site(b["site_id"]):
        return None
    return b


def start_backup(site, note="manual"):
    busy = db.q1("SELECT id FROM backups WHERE site_id=? AND status IN ('running','restoring') AND created>?",
                 (site["id"], int(time.time()) - STALE))
    if busy:
        bad("A backup/restore is already running for this site")
    ts = time.strftime("%Y%m%d-%H%M%S")
    f = _site_dir(site["domain"]) / f"{site['domain']}-{ts}.tar.gz"
    return db.x("INSERT INTO backups(site_id,file,size,status,note,created) VALUES(?,?,0,'running',?,?)",
                (site["id"], str(f), note, int(time.time())))


def _mysqldump(name, out):
    with open(out, "w") as fh:
        p = subprocess.run(["nice", "-n", "19", "mysqldump", f"--defaults-extra-file={config.MYCNF}",
                            "--single-transaction", "--quick", "--routines", "--triggers",
                            "--default-character-set=utf8mb4", name],
                           stdout=fh, stderr=subprocess.PIPE, text=True, timeout=6 * 3600)
    if p.returncode != 0:
        raise RuntimeError(f"mysqldump {name}: {p.stderr[-400:]}")


def run_backup(bid):
    b = _backup(bid)
    site = _site(b["site_id"])
    home = config.HOME_ROOT / site["domain"]
    work = Path(tempfile.mkdtemp(prefix=".work-", dir=str(_site_dir(site["domain"]))))
    try:
        dbs = [r["name"] for r in db.q("SELECT name FROM dbs WHERE site_id=?", (site["id"],))]
        for name in dbs:
            _mysqldump(name, work / f"db_{name}.sql")
        meta = {"domain": site["domain"], "user": site["user"], "php": site["php"], "dbs": dbs,
                "created": int(time.time()), "panel": config.VERSION}
        (work / "ypanel-meta.json").write_text(json.dumps(meta, indent=2))
        cmd = ["nice", "-n", "19", "ionice", "-c3", "tar", "-czf", b["file"]]
        for ex in [e.strip() for e in db.setting("backup_exclude").split(",") if e.strip()]:
            cmd.append(f"--exclude={ex}")
        cmd += ["-C", str(home), "public_html", "-C", str(work)] + sorted(os.listdir(work))
        code, out, _ = run_raw(cmd, timeout=12 * 3600)
        if code not in (0, 1):  # 1 = "file changed as we read it" — acceptable
            raise RuntimeError("tar: " + out[-400:])
        os.chmod(b["file"], 0o600)
        db.x("UPDATE backups SET status='done', size=? WHERE id=?", (os.path.getsize(b["file"]), bid))
        db.log("backup.done", site["domain"])
    except Exception as e:  # noqa: BLE001
        Path(b["file"]).unlink(missing_ok=True)
        db.x("UPDATE backups SET status='failed', note=? WHERE id=?", (str(e)[-500:], bid))
        db.log("backup.failed", f"{site['domain']}: {e}")
        return
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if remote.enabled():
        run_upload(bid)
    prune(site)


def run_upload(bid):
    b = db.q1("SELECT b.*, s.domain FROM backups b JOIN sites s ON s.id=b.site_id WHERE b.id=?", (bid,))
    if not b or not Path(b["file"]).exists():
        return
    db.x("UPDATE backups SET remote='uploading' WHERE id=?", (bid,))
    try:
        remote.upload(b["file"], b["domain"])
        db.x("UPDATE backups SET remote='uploaded' WHERE id=?", (bid,))
        db.log("backup.remote", b["domain"])
    except Exception as e:  # noqa: BLE001
        msg = getattr(e, "detail", None) or getattr(e, "out", None) or str(e)
        db.x("UPDATE backups SET remote=? WHERE id=?", (f"failed: {str(msg)[-300:]}", bid))
        db.log("backup.remote_failed", f"{b['domain']}: {str(msg)[-200:]}")


def run_fetch(bid, domain, name):
    b = _backup(bid)
    try:
        remote.download(domain, name, b["file"])
        os.chmod(b["file"], 0o600)
        db.x("UPDATE backups SET status='done', size=?, note='fetched from remote', remote='uploaded' WHERE id=?",
             (os.path.getsize(b["file"]), bid))
    except Exception as e:  # noqa: BLE001
        Path(b["file"]).unlink(missing_ok=True)
        msg = getattr(e, "detail", None) or getattr(e, "out", None) or str(e)
        db.x("UPDATE backups SET status='failed', note=? WHERE id=?", (f"download: {str(msg)[-300:]}", bid))


def prune(site):
    try:
        keep = max(1, int(db.setting("backup_keep") or 5))
    except ValueError:
        keep = 5
    rows = db.q("SELECT * FROM backups WHERE site_id=? AND status='done' ORDER BY created DESC", (site["id"],))
    for r in rows[keep:]:
        Path(r["file"]).unlink(missing_ok=True)
        db.x("DELETE FROM backups WHERE id=?", (r["id"],))


def run_restore(bid):
    b = _backup(bid)
    site = _site(b["site_id"])
    home, u = config.HOME_ROOT / site["domain"], site["user"]
    work = Path(tempfile.mkdtemp(prefix=".restore-", dir=str(_site_dir(site["domain"]))))
    try:
        run(["tar", "-xzf", b["file"], "-C", str(work)], timeout=12 * 3600)
        if (work / "public_html").is_dir():
            run(["rsync", "-a", "--delete", f"{work}/public_html/", f"{home}/public_html/"], timeout=12 * 3600)
            run(["chown", "-R", f"{u}:{u}", f"{home}/public_html"], timeout=3600)
        for sql in sorted(work.glob("db_*.sql")):
            name = sql.stem[3:]
            if not re.match(r"^[a-z0-9_]{1,64}$", name):
                continue
            run(["mysql", f"--defaults-extra-file={config.MYCNF}", "-e",
                 f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"])
            with open(sql) as fh:
                p = subprocess.run(["mysql", f"--defaults-extra-file={config.MYCNF}", name],
                                   stdin=fh, capture_output=True, text=True, timeout=12 * 3600)
            if p.returncode != 0:
                raise RuntimeError(f"mysql import {name}: {p.stderr[-400:]}")
        db.x("UPDATE backups SET status='done', note=? WHERE id=?",
             (f"restored {time.strftime('%Y-%m-%d %H:%M')}", bid))
        db.log("backup.restore", site["domain"])
    except Exception as e:  # noqa: BLE001
        db.x("UPDATE backups SET status='done', note=? WHERE id=?", (f"restore failed: {str(e)[-400:]}", bid))
        db.log("backup.restore_failed", f"{site['domain']}: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def backup_all():
    for s in db.q("SELECT * FROM sites ORDER BY domain"):
        try:
            run_backup(start_backup(s, "scheduled"))
        except HTTPException:
            continue


# ---------- endpoints ----------
@router.get("")
def list_backups(site_id: Optional[int] = None):
    sql = "SELECT b.*, s.domain FROM backups b LEFT JOIN sites s ON s.id=b.site_id"
    if site_id:
        return access.filter_sites(db.q(sql + " WHERE b.site_id=? ORDER BY b.created DESC", (site_id,)))
    return access.filter_sites(db.q(sql + " ORDER BY b.created DESC LIMIT 300"))


class BackupIn(BaseModel):
    site_id: int


@router.post("")
def create_backup(body: BackupIn, bg: BackgroundTasks):
    bid = start_backup(_site(body.site_id))
    bg.add_task(run_backup, bid)
    return {"id": bid}


@router.get("/remote")
def remote_list(site_id: int):
    if not remote.enabled():
        bad("Remote backups are not configured (Settings → Remote backups)")
    return remote.listing(_site(site_id)["domain"])


class FetchIn(BaseModel):
    site_id: int
    name: str


@router.post("/remote/fetch")
def remote_fetch(body: FetchIn, bg: BackgroundTasks):
    if not remote.enabled():
        bad("Remote backups are not configured")
    site = _site(body.site_id)
    if not re.match(r"^[a-z0-9.-]+-\d{8}-\d{6}\.tar\.gz$", body.name) or not body.name.startswith(site["domain"] + "-"):
        bad("Invalid backup name for this site")
    dest = _site_dir(site["domain"]) / body.name
    if dest.exists():
        bad("This backup is already stored locally")
    bid = db.x("INSERT INTO backups(site_id,file,size,status,note,created) VALUES(?,?,0,'running','downloading…',?)",
               (site["id"], str(dest), int(time.time())))
    bg.add_task(run_fetch, bid, site["domain"], body.name)
    return {"id": bid}


@router.post("/{bid}/upload")
def upload_remote(bid: int, bg: BackgroundTasks):
    if not remote.enabled():
        bad("Remote backups are not configured")
    b = _backup(bid)
    if not b or b["status"] != "done" or not Path(b["file"]).exists():
        bad("Backup not available")
    bg.add_task(run_upload, bid)
    return {"ok": True}


@router.post("/{bid}/restore")
def restore(bid: int, bg: BackgroundTasks):
    b = _backup(bid)
    if not b or b["status"] != "done" or not Path(b["file"]).exists():
        bad("Backup not available")
    busy = db.q1("SELECT id FROM backups WHERE site_id=? AND status IN ('running','restoring')", (b["site_id"],))
    if busy:
        bad("Another backup/restore is running for this site")
    db.x("UPDATE backups SET status='restoring', note='restoring…' WHERE id=?", (bid,))
    bg.add_task(run_restore, bid)
    return {"ok": True}


@router.get("/{bid}/download")
def download(bid: int):
    b = _backup(bid)
    if not b or not Path(b["file"]).exists():
        raise HTTPException(404, "Backup file not found")
    return FileResponse(b["file"], filename=Path(b["file"]).name, media_type="application/gzip")


@router.delete("/{bid}")
def delete_backup(bid: int):
    b = _backup(bid)
    if not b:
        raise HTTPException(404, "Backup not found")
    if b["status"] in ("running", "restoring") and b["created"] > time.time() - STALE:
        bad("Backup is busy")
    Path(b["file"]).unlink(missing_ok=True)
    db.x("DELETE FROM backups WHERE id=?", (bid,))
    return {"ok": True}

"""Cron jobs — each site's crontab runs as its own system user."""
import re
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import access, config, db
from ..system import bad, run
from .sites import get_site

router = APIRouter(prefix="/api/cron", tags=["cron"])

FIELD = r"[0-9A-Za-z*/,\-]+"
SCHED_RE = re.compile(rf"^(@(reboot|yearly|annually|monthly|weekly|daily|hourly)|{FIELD}(\s+{FIELD}){{4}})$")


def _validate(schedule, command):
    schedule = " ".join((schedule or "").split())
    command = (command or "").strip()
    if not SCHED_RE.match(schedule):
        bad("Invalid schedule (5 fields, e.g. */5 * * * *)")
    if not command or "\n" in command or "\r" in command or len(command) > 2000:
        bad("Invalid command")
    return schedule, command


def sync_crontab(site):
    rows = db.q("SELECT * FROM crons WHERE site_id=? AND enabled=1 ORDER BY id", (site["id"],))
    u = site["user"]
    if not rows:
        run(["crontab", "-r", "-u", u], check=False)
        return
    lines = ["# Managed by YPanel — manual edits will be overwritten",
             "SHELL=/bin/bash", "PATH=/usr/local/bin:/usr/bin:/bin", 'MAILTO=""',
             f"HOME={config.HOME_ROOT}/{site['domain']}/tmp"]
    lines += [f"{r['schedule']} {r['command']}  # ypanel:{r['id']}" for r in rows]
    run(["crontab", "-u", u, "-"], input="\n".join(lines) + "\n")


@router.get("")
def list_crons(site_id: Optional[int] = None):
    sql = "SELECT c.*, s.domain FROM crons c JOIN sites s ON s.id=c.site_id"
    if site_id:
        return access.filter_sites(db.q(sql + " WHERE c.site_id=? ORDER BY c.id", (site_id,)))
    return access.filter_sites(db.q(sql + " ORDER BY s.domain, c.id"))


class CronIn(BaseModel):
    site_id: int
    schedule: str
    command: str


@router.post("")
def add_cron(body: CronIn):
    site = get_site(body.site_id)
    schedule, command = _validate(body.schedule, body.command)
    cid = db.x("INSERT INTO crons(site_id,schedule,command,enabled,created) VALUES(?,?,?,1,?)",
               (site["id"], schedule, command, int(time.time())))
    try:
        sync_crontab(site)
    except Exception:
        db.x("DELETE FROM crons WHERE id=?", (cid,))
        raise
    db.log("cron.add", f"{site['domain']}: {schedule} {command[:80]}")
    return {"id": cid}


class CronUpd(BaseModel):
    schedule: str
    command: str
    enabled: bool = True


@router.put("/{cid}")
def update_cron(cid: int, body: CronUpd):
    row = db.q1("SELECT * FROM crons WHERE id=?", (cid,))
    if not row:
        raise HTTPException(404, "Cron job not found")
    schedule, command = _validate(body.schedule, body.command)
    db.x("UPDATE crons SET schedule=?, command=?, enabled=? WHERE id=?",
         (schedule, command, int(body.enabled), cid))
    sync_crontab(get_site(row["site_id"]))
    return {"ok": True}


@router.delete("/{cid}")
def delete_cron(cid: int):
    row = db.q1("SELECT * FROM crons WHERE id=?", (cid,))
    if not row:
        raise HTTPException(404, "Cron job not found")
    db.x("DELETE FROM crons WHERE id=?", (cid,))
    sync_crontab(get_site(row["site_id"]))
    return {"ok": True}

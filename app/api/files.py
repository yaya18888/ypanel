"""File manager — every path is jailed to /home/<domain>."""
import os
import re
import shutil
import stat
from pathlib import Path
from typing import List

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import config, db
from ..system import CmdError, bad, chown_to, run, safe_path
from .sites import get_site

router = APIRouter(prefix="/api/files", tags=["files"])
MAX_EDIT = 5 * 1024 * 1024
MAX_LIST = 5000
ARCHIVES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".gz")


def _ctx(sid, path=""):
    site = get_site(sid)
    root = (config.HOME_ROOT / site["domain"]).resolve()
    if not root.exists():
        bad("Site directory is missing", 404)
    return site, root, safe_path(root, path)


def _rel(root, p):
    r = str(p.relative_to(root))
    return "" if r == "." else r


def _clean_name(name):
    name = os.path.basename((name or "").strip())
    if name in ("", ".", "..") or "/" in name or "\x00" in name:
        bad("Invalid file name")
    return name


@router.get("/{sid}/list")
def list_dir(sid: int, path: str = "public_html"):
    site, root, p = _ctx(sid, path)
    if not p.is_dir():
        bad("Not a directory")
    items = []
    with os.scandir(p) as it:
        for e in it:
            try:
                st = e.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            is_link = stat.S_ISLNK(st.st_mode)
            is_dir = e.is_dir(follow_symlinks=True) if is_link else stat.S_ISDIR(st.st_mode)
            items.append({"name": e.name, "dir": is_dir, "link": is_link, "size": st.st_size,
                          "mtime": int(st.st_mtime), "mode": oct(st.st_mode & 0o7777)[2:]})
            if len(items) >= MAX_LIST:
                break
    items.sort(key=lambda i: (not i["dir"], i["name"].lower()))
    return {"path": _rel(root, p), "items": items, "truncated": len(items) >= MAX_LIST}


@router.get("/{sid}/read")
def read_file(sid: int, path: str):
    _, _, p = _ctx(sid, path)
    if not p.is_file():
        bad("Not a file")
    if p.stat().st_size > MAX_EDIT:
        bad("File too large for the editor (max 5 MB) — download it instead")
    raw = p.read_bytes()
    if b"\x00" in raw[:8192]:
        bad("Binary file — download it instead")
    return {"content": raw.decode("utf-8", errors="replace")}


class WriteIn(BaseModel):
    path: str
    content: str


@router.post("/{sid}/write")
def write(sid: int, body: WriteIn):
    site, root, p = _ctx(sid, body.path)
    if p.is_dir():
        bad("Path is a directory")
    if not p.parent.is_dir():
        bad("Parent folder does not exist")
    existed = p.exists()
    mode = p.stat().st_mode & 0o7777 if existed else 0o644
    tmp = p.with_name(p.name + ".yp-tmp")
    tmp.write_bytes(body.content.encode("utf-8"))
    os.chmod(tmp, mode)
    os.replace(tmp, p)
    chown_to(p, site["user"])
    return {"ok": True}


@router.post("/{sid}/upload")
def upload(sid: int, path: str = Form(""), files: List[UploadFile] = File(...)):
    site, root, d = _ctx(sid, path)
    if not d.is_dir():
        bad("Target is not a directory")
    saved = []
    for f in files:
        name = _clean_name(f.filename)
        target = safe_path(root, _rel(root, d / name))
        with open(target, "wb") as out:
            shutil.copyfileobj(f.file, out, 1024 * 1024)
        os.chmod(target, 0o644)
        chown_to(target, site["user"])
        saved.append(name)
    db.log("files.upload", f"{site['domain']}: {len(saved)} file(s)")
    return {"saved": saved}


class PathIn(BaseModel):
    path: str
    name: str = ""


@router.post("/{sid}/mkdir")
def mkdir(sid: int, body: PathIn):
    site, root, d = _ctx(sid, body.path)
    target = safe_path(root, _rel(root, d / _clean_name(body.name)))
    if target.exists():
        bad("Already exists")
    target.mkdir(mode=0o755)
    chown_to(target, site["user"])
    return {"ok": True}


@router.post("/{sid}/newfile")
def newfile(sid: int, body: PathIn):
    site, root, d = _ctx(sid, body.path)
    target = safe_path(root, _rel(root, d / _clean_name(body.name)))
    if target.exists():
        bad("Already exists")
    target.touch(mode=0o644)
    chown_to(target, site["user"])
    return {"ok": True}


class RenameIn(BaseModel):
    path: str
    new_name: str


@router.post("/{sid}/rename")
def rename(sid: int, body: RenameIn):
    site, root, p = _ctx(sid, body.path)
    if p == root:
        bad("Cannot rename the site root")
    dst = safe_path(root, _rel(root, p.parent / _clean_name(body.new_name)))
    if dst.exists():
        bad("Target already exists")
    os.rename(p, dst)
    return {"ok": True}


class MultiIn(BaseModel):
    path: str
    items: List[str]


@router.post("/{sid}/delete")
def delete(sid: int, body: MultiIn):
    site, root, d = _ctx(sid, body.path)
    protected = {root, root / "public_html", root / "logs", root / "tmp"}
    n = 0
    for name in body.items:
        target = d / _clean_name(name)
        if target.is_symlink():
            target.unlink()
            n += 1
            continue
        target = safe_path(root, _rel(root, target))
        if target in protected:
            bad(f"{name} is protected")
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        n += 1
    db.log("files.delete", f"{site['domain']}: {n} item(s) in /{_rel(root, d)}")
    return {"deleted": n}


class ChmodIn(BaseModel):
    path: str
    mode: str


@router.post("/{sid}/chmod")
def chmod(sid: int, body: ChmodIn):
    _, root, p = _ctx(sid, body.path)
    if not re.match(r"^[0-7]{3,4}$", body.mode):
        bad("Mode must be octal like 644 or 755")
    if p.is_symlink():
        bad("Cannot chmod a symlink")
    os.chmod(p, int(body.mode, 8))
    return {"ok": True}


@router.post("/{sid}/extract")
def extract(sid: int, body: PathIn):
    site, root, p = _ctx(sid, body.path)
    if not p.is_file():
        bad("Not a file")
    name = p.name.lower()
    dest = str(p.parent)
    if name.endswith(".zip"):
        cmd = ["unzip", "-o", "-q", str(p), "-d", dest]
    elif name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")):
        cmd = ["tar", "-xf", str(p), "-C", dest, "--no-same-owner"]
    elif name.endswith(".gz"):
        cmd = ["gunzip", "-k", "-f", str(p)]
    else:
        bad("Unsupported archive type")
    try:
        run(cmd, as_user=site["user"], timeout=3600)  # runs as the site user — can't escape its perms
    except CmdError as e:
        bad("Extract failed:\n" + e.out[-800:])
    db.log("files.extract", f"{site['domain']}: {p.name}")
    return {"ok": True}


class CompressIn(BaseModel):
    path: str
    items: List[str]
    name: str = "archive.zip"


@router.post("/{sid}/compress")
def compress(sid: int, body: CompressIn):
    site, root, d = _ctx(sid, body.path)
    out = _clean_name(body.name)
    if not out.endswith(".zip"):
        out += ".zip"
    if not body.items:
        bad("Nothing selected")
    items = []
    for i in body.items:
        safe_path(root, _rel(root, d / _clean_name(i)))
        items.append("./" + _clean_name(i))
    try:
        run(["zip", "-r", "-q", "-y", out, *items], as_user=site["user"], cwd=str(d), timeout=3600)
    except CmdError as e:
        bad("Compress failed:\n" + e.out[-800:])
    return {"ok": True, "file": out}


@router.get("/{sid}/download")
def download(sid: int, path: str):
    _, _, p = _ctx(sid, path)
    if not p.is_file():
        bad("Not a file")
    return FileResponse(str(p), filename=p.name, media_type="application/octet-stream")

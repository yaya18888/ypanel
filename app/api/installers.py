"""1-click installers: WordPress, Git deploy, clone files from another site."""
import os
import re
import secrets
import shutil
import time

from fastapi import APIRouter
from pydantic import BaseModel

from .. import config, db
from ..system import CmdError, bad, run, which
from .databases import create_database, drop_database
from .sites import get_site

router = APIRouter(prefix="/api/install", tags=["installers"])
KEEP = {"index.html", ".well-known"}


def _prepare_docroot(site, force):
    doc = config.HOME_ROOT / site["domain"] / "public_html"
    entries = [e for e in os.listdir(doc) if e not in KEEP]
    if entries and not force:
        bad("public_html is not empty — tick 'overwrite' to wipe it first")
    for e in os.listdir(doc):
        p = doc / e
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
    return doc


class WpIn(BaseModel):
    site_id: int
    title: str
    admin_user: str
    admin_password: str
    admin_email: str
    locale: str = "en_US"
    force: bool = False


@router.post("/wordpress")
def wordpress(b: WpIn):
    if not which("wp"):
        bad("wp-cli is not installed (/usr/local/bin/wp)")
    if not re.match(r"^[A-Za-z0-9_.@-]{3,60}$", b.admin_user):
        bad("Admin username: 3-60 chars, letters/digits/_ . @ -")
    if len(b.admin_password) < 10:
        bad("Admin password must be at least 10 characters")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", b.admin_email):
        bad("Invalid admin email")
    if not re.match(r"^[a-z]{2,3}(_[A-Z]{2})?$", b.locale):
        bad("Invalid locale (e.g. en_US, fr_FR, ar)")
    if not b.title.strip() or len(b.title) > 200:
        bad("Invalid site title")
    site = get_site(b.site_id)
    home = config.HOME_ROOT / site["domain"]
    doc = _prepare_docroot(site, b.force)
    creds = create_database(site, "wp" + secrets.token_hex(2))
    scheme = "https" if site["ssl"] else "http"
    url = f"{scheme}://{site['domain']}"

    def wp(*args):
        return run(["env", f"HOME={home}/tmp", "wp", *args, f"--path={doc}"], as_user=site["user"], timeout=900)

    try:
        wp("core", "download", f"--locale={b.locale}")
        wp("config", "create", f"--dbname={creds['name']}", f"--dbuser={creds['user']}",
           f"--dbpass={creds['password']}", "--dbhost=localhost", "--skip-check")
        wp("core", "install", f"--url={url}", f"--title={b.title.strip()}", f"--admin_user={b.admin_user}",
           f"--admin_password={b.admin_password}", f"--admin_email={b.admin_email}", "--skip-email")
        run(["chmod", "640", str(doc / "wp-config.php")])
    except CmdError as e:
        drop_database(creds["name"])
        db.x("DELETE FROM dbs WHERE name=?", (creds["name"],))
        bad("WordPress install failed:\n" + e.out[-1000:])
    db.log("install.wordpress", site["domain"])
    return {"url": url, "admin_url": f"{url}/wp-admin/", "db": creds["name"]}


class GitIn(BaseModel):
    site_id: int
    repo: str
    branch: str = ""
    force: bool = False


@router.post("/git")
def git_deploy(b: GitIn):
    if not re.match(r"^https://[\w.@:/~+%-]+$", b.repo.strip()):
        bad("Repository must be an https:// URL (use a token in the URL for private repos)")
    if b.branch and not re.match(r"^[\w./-]{1,100}$", b.branch):
        bad("Invalid branch name")
    site = get_site(b.site_id)
    home = config.HOME_ROOT / site["domain"]
    doc = _prepare_docroot(site, b.force)
    tmp = home / "tmp" / f"git-{int(time.time())}"
    cmd = ["env", f"HOME={home}/tmp", "git", "clone", "--depth", "1"]
    if b.branch:
        cmd += ["-b", b.branch]
    try:
        run(cmd + [b.repo.strip(), str(tmp)], as_user=site["user"], timeout=1800)
        run(["rsync", "-a", f"{tmp}/", f"{doc}/"], as_user=site["user"], timeout=1800)
    except CmdError as e:
        bad("Git deploy failed:\n" + re.sub(r"https://[^@\s]+@", "https://***@", e.out[-1000:]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    db.log("install.git", site["domain"])
    return {"ok": True}


class CloneIn(BaseModel):
    source_id: int
    target_id: int
    exclude: str = ""
    force: bool = False


@router.post("/clone")
def clone_files(b: CloneIn):
    if b.source_id == b.target_id:
        bad("Source and target must differ")
    src, dst = get_site(b.source_id), get_site(b.target_id)
    doc = _prepare_docroot(dst, b.force)
    cmd = ["rsync", "-a"]
    for ex in [e.strip() for e in b.exclude.split(",") if e.strip()]:
        if ".." in ex:
            bad("Invalid exclude pattern")
        cmd.append(f"--exclude={ex}")
    cmd += [f"{config.HOME_ROOT / src['domain']}/public_html/", f"{doc}/"]
    run(cmd, timeout=6 * 3600)
    run(["chown", "-R", f"{dst['user']}:{dst['user']}", str(doc)], timeout=3600)
    db.log("install.clone", f"{src['domain']} → {dst['domain']}")
    return {"ok": True}

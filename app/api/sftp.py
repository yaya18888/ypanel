"""Chrooted SFTP access per site (login = the site's system user, jailed to /home/<domain>)."""
import grp
import os
import pwd
import re

from fastapi import APIRouter
from pydantic import BaseModel

from .. import config, db
from ..system import bad, detect_ip, gen_password, run, run_raw, write_file
from .sites import get_site

router = APIRouter(prefix="/api/sftp", tags=["sftp"])
KEY_RE = re.compile(r"^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-nistp\d+|sk-[\w@.-]+) [A-Za-z0-9+/=]+( .{0,200})?$")


def ssh_port():
    code, out, _ = run_raw(["sshd", "-T"])
    m = re.search(r"^port (\d+)", out, re.M) if code == 0 else None
    return int(m.group(1)) if m else 22


def sftp_ready():
    try:
        grp.getgrnam(config.SFTP_GROUP)
        return True
    except KeyError:
        return False


def chroot_home(site):
    """sshd ChrootDirectory requires a root-owned, non group-writable home."""
    home = config.HOME_ROOT / site["domain"]
    gid = pwd.getpwnam(site["user"]).pw_gid
    os.chown(home, 0, gid)
    os.chmod(home, 0o750)


def _key_file(user):
    return config.SFTP_KEYS / user


def _write_keys(user, keys):
    lines = [k.strip() for k in (keys or "").splitlines() if k.strip()]
    for k in lines:
        if not KEY_RE.match(k):
            bad(f"Invalid public key: {k[:40]}…")
    config.SFTP_KEYS.mkdir(mode=0o755, exist_ok=True)
    if lines:
        write_file(_key_file(user), "\n".join(lines) + "\n", mode=0o644)
    else:
        _key_file(user).unlink(missing_ok=True)
    return len(lines)


def _set_password(user, pw):
    if len(pw) < 10 or ":" in pw or "\n" in pw:
        bad("Password: at least 10 characters, no ':'")
    run(["chpasswd"], input=f"{user}:{pw}\n")


def remove_access(user):
    run(["gpasswd", "-d", user, config.SFTP_GROUP], check=False)
    run(["usermod", "-L", user], check=False)
    _key_file(user).unlink(missing_ok=True)


def _info(site):
    kf = _key_file(site["user"])
    return {"enabled": bool(site.get("sftp")), "ready": sftp_ready(), "user": site["user"],
            "host": detect_ip(), "port": ssh_port(), "path": "/public_html",
            "keys": kf.read_text() if kf.exists() else ""}


@router.get("/{sid}")
def info(sid: int):
    return _info(get_site(sid))


class EnableIn(BaseModel):
    password: str = ""
    keys: str = ""


@router.post("/{sid}/enable")
def enable(sid: int, body: EnableIn):
    site = get_site(sid)
    if not sftp_ready():
        bad("SFTP is not configured on this server — re-run install.sh (v2)")
    pw = body.password or gen_password(18)
    if len(pw) < 10 or ":" in pw or "\n" in pw:
        bad("Password: at least 10 characters, no ':'")
    _write_keys(site["user"], body.keys)  # validates keys before anything changes
    chroot_home(site)
    _set_password(site["user"], pw)
    run(["usermod", "-aG", config.SFTP_GROUP, site["user"]])
    db.x("UPDATE sites SET sftp=1 WHERE id=?", (sid,))
    db.log("sftp.enable", site["domain"])
    site["sftp"] = 1
    return {**_info(site), "password": pw}


class PwIn(BaseModel):
    password: str = ""


@router.post("/{sid}/password")
def password(sid: int, body: PwIn):
    site = get_site(sid)
    if not site.get("sftp"):
        bad("SFTP is disabled for this site")
    pw = body.password or gen_password(18)
    _set_password(site["user"], pw)
    db.log("sftp.password", site["domain"])
    return {"password": pw}


class KeysIn(BaseModel):
    keys: str = ""


@router.put("/{sid}/keys")
def keys(sid: int, body: KeysIn):
    site = get_site(sid)
    n = _write_keys(site["user"], body.keys)
    db.log("sftp.keys", f"{site['domain']}: {n} key(s)")
    return {"keys": n}


@router.post("/{sid}/disable")
def disable(sid: int):
    site = get_site(sid)
    remove_access(site["user"])
    db.x("UPDATE sites SET sftp=0 WHERE id=?", (sid,))
    db.log("sftp.disable", site["domain"])
    return {"ok": True}

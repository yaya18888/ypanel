"""Shell helpers, validators, path safety."""
import os
import pwd
import re
import secrets
import shutil
import string
import subprocess
from pathlib import Path

from fastapi import HTTPException

from . import config, db

DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}$")
NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")
SIZE_RE = re.compile(r"^\d{1,5}[KMG]$")


class CmdError(Exception):
    def __init__(self, cmd, code, out):
        self.cmd, self.code, self.out = cmd, code, out
        super().__init__(f"{' '.join(map(str, cmd[:4]))} … exited {code}: {out[-800:]}")


def bad(msg, code=400):
    raise HTTPException(code, msg)


def run_raw(cmd, *, input=None, timeout=300, as_user=None, cwd=None, env=None):
    cmd = [str(c) for c in cmd]
    if as_user:
        cmd = ["runuser", "-u", as_user, "--"] + cmd
    if env:
        env = {**os.environ, **env}
    try:
        p = subprocess.run(cmd, input=input, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)
    except FileNotFoundError:
        return 127, f"{cmd[0]}: command not found", cmd
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]}: timed out after {timeout}s", cmd
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip(), cmd


def run(cmd, *, input=None, timeout=300, check=True, as_user=None, cwd=None, env=None):
    code, out, full = run_raw(cmd, input=input, timeout=timeout, as_user=as_user, cwd=cwd, env=env)
    if check and code != 0:
        raise CmdError(full, code, out)
    return out


def norm_domain(d):
    d = (d or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0].split(":")[0]
    if d.startswith("www."):
        d = d[4:]
    if not DOMAIN_RE.match(d):
        bad("Invalid domain name")
    return d


def gen_password(n=20):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def user_exists(u):
    try:
        pwd.getpwnam(u)
        return True
    except KeyError:
        return False


def make_site_user(domain):
    base = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])[:8] or "site"
    if not base[0].isalpha():
        base = ("s" + base)[:8]
    for _ in range(30):
        u = base + secrets.token_hex(2)
        if not user_exists(u):
            return u
    bad("Could not allocate a system user")


def chown_to(path, user):
    try:
        pw = pwd.getpwnam(user)
        os.chown(path, pw.pw_uid, pw.pw_gid, follow_symlinks=False)
    except KeyError:
        pass


def php_versions():
    vs = [p.parent.name for p in Path("/etc/php").glob("*/fpm") if p.is_dir()]
    return sorted(vs, key=lambda v: tuple(int(x) for x in v.split(".") if x.isdigit()))


def default_php():
    vs = php_versions()
    want = db.setting("default_php")
    if want in vs:
        return want
    return vs[-1] if vs else "8.3"


def safe_path(base, rel):
    base = Path(base).resolve()
    p = (base / str(rel or "").lstrip("/")).resolve()
    if p != base and base not in p.parents:
        bad("Path is outside the site directory", 403)
    return p


def write_file(path, content, mode=0o644, owner=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".yp-tmp")
    tmp.write_text(content)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    if owner:
        chown_to(path, owner)


def nginx_apply(rollback=None):
    code, out, _ = run_raw(["nginx", "-t"])
    if code != 0:
        if rollback:
            rollback()
        bad("nginx config test failed:\n" + out[-800:])
    run(["systemctl", "reload", "nginx"])


def has_ipv6():
    return Path("/proc/net/if_inet6").exists()


def size_mb(v, default=64):
    m = re.match(r"^(\d+)([KMG]?)$", str(v).upper())
    if not m:
        return default
    n, unit = int(m.group(1)), m.group(2)
    return max(1, {"K": n // 1024, "M": n, "G": n * 1024, "": n // (1024 * 1024)}[unit])


def detect_ip():
    ip = db.setting("server_ip")
    if ip:
        return ip
    code, out, _ = run_raw(["ip", "-4", "route", "get", "1.1.1.1"])
    m = re.search(r"src (\d+\.\d+\.\d+\.\d+)", out) if code == 0 else None
    return m.group(1) if m else ""


def tail(path, lines=200):
    p = Path(path)
    if not p.exists():
        return ""
    return run(["tail", "-n", str(int(lines)), str(p)], check=False)


def which(cmd):
    return shutil.which(cmd) is not None

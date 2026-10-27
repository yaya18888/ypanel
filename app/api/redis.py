"""Redis object cache — one ACL user per site, keys jailed to '<siteuser>:*'."""
import re

from fastapi import APIRouter
from pydantic import BaseModel

from .. import access, config, db
from ..system import CmdError, bad, gen_password, run, run_raw, which
from .sites import get_site

router = APIRouter(prefix="/api/redis", tags=["redis"])
PASS_FILE = config.DATA / "redis.pass"
HOST, PORT = "127.0.0.1", 6379


def available():
    return which("redis-cli") and PASS_FILE.exists()


def cli(*args, check=True, timeout=60):
    if not available():
        bad("Redis is not installed — re-run install.sh (v3)")
    code, out, _ = run_raw(["redis-cli", "-h", HOST, "-p", str(PORT), "--no-auth-warning", *args],
                           env={"REDISCLI_AUTH": PASS_FILE.read_text().strip()}, timeout=timeout)
    if check and (code != 0 or out.startswith(("ERR", "(error)", "NOPERM", "WRONGPASS"))):
        bad("Redis: " + out[-400:])
    return out


def _acl_user(site):
    return f"yp_{site['user']}"


def _prefix(site):
    return f"{site['user']}:"


def _is_wp(site):
    return (config.HOME_ROOT / site["domain"] / "public_html" / "wp-config.php").exists()


def _wp(site, *args, check=True):
    home = config.HOME_ROOT / site["domain"]
    cmd = ["env", f"HOME={home}/tmp", "wp", *args, f"--path={home}/public_html"]
    if check:
        return run(cmd, as_user=site["user"], timeout=300)
    return run_raw(cmd, as_user=site["user"], timeout=300)


def parse_info(text):
    info = {}
    for line in text.splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()
    return info


# ---------- server (admin) ----------
@router.get("/server")
def server_info():
    access.require_admin()
    if not available():
        return {"available": False}
    info = parse_info(cli("INFO"))
    keys = sum(int(m.group(1)) for k, v in info.items() if k.startswith("db")
               for m in [re.search(r"keys=(\d+)", v)] if m)
    return {"available": True, "version": info.get("redis_version"), "used": info.get("used_memory_human"),
            "maxmemory": info.get("maxmemory_human"), "policy": info.get("maxmemory_policy"),
            "clients": info.get("connected_clients"), "keys": keys,
            "hits": int(info.get("keyspace_hits", 0)), "misses": int(info.get("keyspace_misses", 0)),
            "sites": db.q1("SELECT COUNT(*) n FROM sites WHERE redis_user<>''")["n"]}


class MemIn(BaseModel):
    maxmemory: str


@router.put("/server")
def set_maxmemory(b: MemIn):
    access.require_admin()
    v = b.maxmemory.strip().lower()
    if not re.match(r"^\d{2,5}mb$|^\d{1,2}gb$", v):
        bad("Use a value like 128mb, 256mb or 1gb")
    cli("CONFIG", "SET", "maxmemory", v)
    cli("CONFIG", "REWRITE", check=False)
    db.set_setting("redis_maxmemory", v)
    db.log("redis.maxmemory", v)
    return {"ok": True}


# ---------- per site ----------
@router.get("/{sid}")
def site_info(sid: int):
    s = get_site(sid)
    wp = _is_wp(s)
    active = False
    if wp and s.get("redis_user"):
        code, out, _ = _wp(s, "redis", "status", check=False)
        active = code == 0 and "Status: Connected" in out
    return {"available": bool(available()), "enabled": bool(s.get("redis_user")), "host": HOST, "port": PORT,
            "username": s.get("redis_user") or "", "password": s.get("redis_pass") or "",
            "prefix": _prefix(s), "wordpress": wp, "wp_active": active}


@router.post("/{sid}/enable")
def enable(sid: int):
    s = get_site(sid)
    user, pw = _acl_user(s), gen_password(32)
    cli("ACL", "SETUSER", user, "reset", "on", f">{pw}", f"~{_prefix(s)}*", "+@all", "-@dangerous", "+info")
    cli("ACL", "SAVE", check=False)
    db.x("UPDATE sites SET redis_user=?, redis_pass=? WHERE id=?", (user, pw, sid))
    db.log("redis.enable", s["domain"])
    return site_info(sid)


@router.post("/{sid}/wordpress")
def wordpress(sid: int):
    s = get_site(sid)
    if not s.get("redis_user"):
        bad("Enable Redis for this site first")
    if not _is_wp(s):
        bad("No wp-config.php in public_html")
    if not which("wp"):
        bad("wp-cli is not installed")
    try:
        for k, v, raw in (("WP_REDIS_HOST", HOST, False), ("WP_REDIS_PORT", str(PORT), True),
                          ("WP_REDIS_PREFIX", _prefix(s), False),
                          ("WP_REDIS_PASSWORD", f"['{s['redis_user']}', '{s['redis_pass']}']", True),
                          ("WP_REDIS_SELECTIVE_FLUSH", "true", True), ("WP_REDIS_TIMEOUT", "1", True),
                          ("WP_REDIS_READ_TIMEOUT", "1", True)):
            _wp(s, "config", "set", k, v, *(["--raw"] if raw else []), "--type=constant")
        _wp(s, "plugin", "install", "redis-cache", "--activate")
        _wp(s, "redis", "enable", "--force")
    except CmdError as e:
        bad("WordPress setup failed:\n" + e.out[-800:])
    db.log("redis.wordpress", s["domain"])
    return site_info(sid)


@router.post("/{sid}/flush")
def flush(sid: int):
    s = get_site(sid)
    if not s.get("redis_user"):
        bad("Redis is not enabled for this site")
    keys = [k for k in cli("--scan", "--pattern", f"{_prefix(s)}*", timeout=300).splitlines() if k.strip()]
    for i in range(0, len(keys), 500):
        cli("UNLINK", *keys[i:i + 500])
    db.log("redis.flush", f"{s['domain']}: {len(keys)} keys")
    return {"deleted": len(keys)}


@router.post("/{sid}/disable")
def disable(sid: int):
    s = get_site(sid)
    if _is_wp(s) and which("wp"):
        _wp(s, "redis", "disable", check=False)
        for k in ("WP_REDIS_HOST", "WP_REDIS_PORT", "WP_REDIS_PREFIX", "WP_REDIS_PASSWORD",
                  "WP_REDIS_SELECTIVE_FLUSH", "WP_REDIS_TIMEOUT", "WP_REDIS_READ_TIMEOUT"):
            _wp(s, "config", "delete", k, "--type=constant", check=False)
    if s.get("redis_user") and available():
        try:
            flush(sid)
        except Exception:  # noqa: BLE001
            pass
        cli("ACL", "DELUSER", s["redis_user"], check=False)
        cli("ACL", "SAVE", check=False)
    db.x("UPDATE sites SET redis_user='', redis_pass='' WHERE id=?", (sid,))
    db.log("redis.disable", s["domain"])
    return {"ok": True}


def drop_site_user(site):
    """Called when a site is deleted."""
    if site.get("redis_user") and available():
        cli("ACL", "DELUSER", site["redis_user"], check=False)
        cli("ACL", "SAVE", check=False)


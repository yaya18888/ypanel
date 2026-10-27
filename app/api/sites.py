"""Websites: system user + PHP-FPM pool + nginx vhost + SSL."""
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import access, config, db, templates
from ..system import (CmdError, SIZE_RE, bad, default_php, make_site_user, nginx_apply,
                      norm_domain, php_versions, run, run_raw, tail, write_file)

router = APIRouter(prefix="/api/sites", tags=["sites"])

DEFAULT_INDEX = """<!doctype html><html><head><meta charset="utf-8"><title>{d}</title>
<style>body{{font-family:system-ui;background:#0f1115;color:#e6e8ee;display:grid;place-items:center;height:100vh;margin:0}}</style>
</head><body><div><h1>{d}</h1><p>Site is ready — upload your files to public_html.</p></div></body></html>
"""


# ---------- helpers ----------
def get_site(sid):
    access.assert_site(sid)
    s = db.q1("SELECT * FROM sites WHERE id=?", (sid,))
    if not s:
        raise HTTPException(404, "Site not found")
    s["ini"] = {**templates.INI_DEFAULTS, **json.loads(s.get("ini") or "{}")}
    return s


def site_home(site):
    return config.HOME_ROOT / site["domain"]


def _vhost_path(d):
    return config.NGINX_SITES / f"{d}.conf"


def _pool_path(user, ver):
    return Path(f"/etc/php/{ver}/fpm/pool.d/{user}.conf")


def _custom_path(d):
    return config.NGINX_CUSTOM / f"{d}.conf"


def apply_vhost(site):
    d = site["domain"]
    path, link = _vhost_path(d), config.NGINX_ENABLED / f"{d}.conf"
    old = path.read_text() if path.exists() else None
    cp = _custom_path(d)
    if not cp.exists():
        write_file(cp, f"# Custom nginx directives for {d} (server context)\n")
    write_file(path, templates.vhost(site))
    if not link.exists():
        link.symlink_to(path)

    def rollback():
        if old is None:
            link.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
        else:
            write_file(path, old)

    nginx_apply(rollback)


def apply_pool(site):
    ver = site["php"]
    path = _pool_path(site["user"], ver)
    old = path.read_text() if path.exists() else None
    write_file(path, templates.fpm_pool(site))
    code, out, _ = run_raw([f"php-fpm{ver}", "-t"])
    if code != 0:
        if old is None:
            path.unlink(missing_ok=True)
        else:
            write_file(path, old)
        bad("PHP-FPM config test failed:\n" + out[-600:])
    run(["systemctl", "reload", f"php{ver}-fpm"])
    time.sleep(0.5)  # let the new socket appear before nginx points at it


def remove_pool(user, ver):
    p = _pool_path(user, ver)
    if p.exists():
        p.unlink()
        run(["systemctl", "reload", f"php{ver}-fpm"], check=False)


def cert_info(d):
    p = config.LE_LIVE / d / "fullchain.pem"
    if not p.exists():
        return None
    out = run(["openssl", "x509", "-enddate", "-noout", "-in", str(p)], check=False)
    m = re.search(r"notAfter=(.+)", out)
    return {"expires": m.group(1).strip() if m else "?"}


def destroy(site, delete_files=True, delete_dbs=True):
    d, u = site["domain"], site["user"]
    (config.NGINX_ENABLED / f"{d}.conf").unlink(missing_ok=True)
    _vhost_path(d).unlink(missing_ok=True)
    _custom_path(d).unlink(missing_ok=True)
    if run_raw(["nginx", "-t"])[0] == 0:
        run(["systemctl", "reload", "nginx"], check=False)
    for v in php_versions():
        remove_pool(u, v)
    if (config.LE_LIVE / d).exists():
        run(["certbot", "delete", "--cert-name", d, "--non-interactive"], check=False)
    run(["crontab", "-r", "-u", u], check=False)
    try:
        from .redis import drop_site_user
        drop_site_user(site)
    except Exception:  # noqa: BLE001
        pass
    if delete_dbs and site.get("id"):
        from .databases import drop_database
        for row in db.q("SELECT name FROM dbs WHERE site_id=?", (site["id"],)):
            try:
                drop_database(row["name"])
            except Exception:
                pass
        db.x("DELETE FROM dbs WHERE site_id=?", (site["id"],))
    run(["gpasswd", "-d", "www-data", u], check=False)
    run(["gpasswd", "-d", u, config.SFTP_GROUP], check=False)
    (config.SFTP_KEYS / u).unlink(missing_ok=True)
    run(["userdel", u], check=False)
    run(["groupdel", u], check=False)
    if delete_files:
        shutil.rmtree(config.HOME_ROOT / d, ignore_errors=True)


def issue_ssl(site, method="http"):
    d = site["domain"]
    email = db.setting("admin_email")
    if not email:
        bad("Set the admin email in Settings first (Let's Encrypt needs it).")
    cmd = ["certbot", "certonly", "--non-interactive", "--agree-tos", "-m", email,
           "--cert-name", d, "--key-type", "ecdsa", "--keep-until-expiring",
           "--deploy-hook", "systemctl reload nginx"]
    if method == "cloudflare":
        if not config.CF_INI.exists():
            bad("Cloudflare API token is not configured (Settings → Cloudflare).")
        cmd += ["--dns-cloudflare", "--dns-cloudflare-credentials", str(config.CF_INI),
                "--dns-cloudflare-propagation-seconds", "30"]
    else:
        cmd += ["--webroot", "-w", str(config.ACME_ROOT)]
    cmd += ["-d", d] + (["-d", f"www.{d}"] if site["www"] else [])
    try:
        run(cmd, timeout=900)
    except CmdError as e:
        bad("Certbot failed:\n" + e.out[-1500:])
    db.x("UPDATE sites SET ssl=1 WHERE id=?", (site["id"],))
    site["ssl"] = 1
    apply_vhost(site)
    db.log("ssl.issue", f"{d} ({method})")


# ---------- endpoints ----------
class SiteIn(BaseModel):
    domain: str
    www: bool = True
    php: Optional[str] = None
    cloudflare_dns: bool = False
    cf_proxied: bool = True
    ssl: bool = False
    ssl_method: str = "http"


@router.get("")
def list_sites():
    rows = access.filter_sites(db.q("SELECT id,domain,user,php,www,ssl,sftp,created FROM sites ORDER BY domain"),
                               key="id")
    for r in rows:
        r["cert"] = cert_info(r["domain"]) if r["ssl"] else None
    return rows


@router.post("")
def create_site(body: SiteIn):
    access.require_admin()
    d = norm_domain(body.domain)
    if db.q1("SELECT id FROM sites WHERE domain=?", (d,)):
        bad("This site already exists")
    php = body.php or default_php()
    if php not in php_versions():
        bad(f"PHP {php} is not installed")
    home = config.HOME_ROOT / d
    if home.exists():
        bad(f"{home} already exists on disk")
    user = make_site_user(d)
    site = {"id": None, "domain": d, "user": user, "php": php, "www": int(body.www), "ssl": 0,
            "ini": dict(templates.INI_DEFAULTS)}
    try:
        run(["useradd", "-M", "-d", str(home), "-s", "/usr/sbin/nologin", "-U", user])
        for sub in ("public_html", "logs", "tmp"):
            (home / sub).mkdir(parents=True, exist_ok=True)
        (home / "public_html" / "index.html").write_text(DEFAULT_INDEX.format(d=d))
        run(["chown", "-R", f"{user}:{user}", str(home)])
        run(["chown", f"root:{user}", str(home)])  # root-owned home = SFTP-chroot ready
        os.chmod(home, 0o750)
        os.chmod(home / "public_html", 0o750)
        os.chmod(home / "logs", 0o750)
        os.chmod(home / "tmp", 0o700)
        run(["usermod", "-aG", user, "www-data"])
        apply_pool(site)
        apply_vhost(site)
        sid = db.x("INSERT INTO sites(domain,user,php,www,ssl,ini,created) VALUES(?,?,?,?,0,?,?)",
                   (d, user, php, int(body.www), json.dumps(site["ini"]), int(time.time())))
        site["id"] = sid
    except Exception:
        destroy(site, delete_files=True, delete_dbs=False)
        raise

    db.log("site.create", d)
    warnings = []
    if body.cloudflare_dns:
        try:
            from .cloudflare import setup_site_dns
            setup_site_dns(d, bool(body.www), body.cf_proxied)
        except HTTPException as e:
            warnings.append(f"Cloudflare DNS: {e.detail}")
        except Exception as e:  # noqa: BLE001
            warnings.append(f"Cloudflare DNS: {e}")
    if body.ssl:
        try:
            issue_ssl(site, body.ssl_method)
        except HTTPException as e:
            warnings.append(f"SSL: {e.detail}")
    return {"id": sid, "domain": d, "user": user, "warnings": warnings}


@router.get("/{sid}")
def site_detail(sid: int):
    s = get_site(sid)
    home = site_home(s)
    s["home"] = str(home)
    s["docroot"] = str(home / "public_html")
    s["cert"] = cert_info(s["domain"])
    s["php_versions"] = php_versions()
    s["dbs"] = db.q("SELECT name FROM dbs WHERE site_id=?", (sid,))
    return s


@router.get("/{sid}/usage")
def site_usage(sid: int):
    s = get_site(sid)
    out = run(["du", "-sb", str(site_home(s))], check=False, timeout=120)
    try:
        return {"bytes": int(out.split()[0])}
    except (ValueError, IndexError):
        return {"bytes": None}


class PhpIn(BaseModel):
    version: str


@router.put("/{sid}/php")
def change_php(sid: int, body: PhpIn):
    s = get_site(sid)
    if body.version not in php_versions():
        bad("PHP version not installed")
    if body.version == s["php"]:
        return {"ok": True}
    old = s["php"]
    s["php"] = body.version
    apply_pool(s)
    try:
        apply_vhost(s)
    except HTTPException:
        remove_pool(s["user"], body.version)
        raise
    remove_pool(s["user"], old)
    db.x("UPDATE sites SET php=? WHERE id=?", (body.version, sid))
    db.log("site.php", f"{s['domain']} {old} → {body.version}")
    return {"ok": True}


class IniIn(BaseModel):
    memory_limit: str = "256M"
    upload_max_filesize: str = "128M"
    post_max_size: str = "128M"
    max_execution_time: int = 120
    max_input_time: int = 120
    max_input_vars: int = 5000
    max_children: int = 5
    open_basedir: bool = True
    display_errors: bool = False


@router.put("/{sid}/ini")
def update_ini(sid: int, body: IniIn):
    s = get_site(sid)
    data = body.model_dump()
    if not access.is_admin():
        data["open_basedir"] = s["ini"].get("open_basedir", True)  # isolation is admin-controlled
    for k in ("memory_limit", "upload_max_filesize", "post_max_size"):
        data[k] = data[k].strip().upper()
        if not SIZE_RE.match(data[k]):
            bad(f"{k}: use a value like 256M or 1G")
    if not (1 <= data["max_children"] <= 100):
        bad("max_children must be 1-100")
    if not (5 <= data["max_execution_time"] <= 3600):
        bad("max_execution_time must be 5-3600")
    s["ini"] = data
    apply_pool(s)
    apply_vhost(s)
    db.x("UPDATE sites SET ini=? WHERE id=?", (json.dumps(data), sid))
    db.log("site.ini", s["domain"])
    return {"ok": True}


class SslIn(BaseModel):
    method: str = "http"


@router.post("/{sid}/ssl")
def site_ssl(sid: int, body: SslIn):
    s = get_site(sid)
    if body.method not in ("http", "cloudflare"):
        bad("method must be http or cloudflare")
    issue_ssl(s, body.method)
    return {"ok": True, "cert": cert_info(s["domain"])}


@router.delete("/{sid}/ssl")
def site_ssl_remove(sid: int):
    s = get_site(sid)
    s["ssl"] = 0
    apply_vhost(s)
    run(["certbot", "delete", "--cert-name", s["domain"], "--non-interactive"], check=False)
    db.x("UPDATE sites SET ssl=0 WHERE id=?", (sid,))
    db.log("ssl.remove", s["domain"])
    return {"ok": True}


class TextIn(BaseModel):
    content: str


@router.get("/{sid}/nginx")
def get_custom_nginx(sid: int):
    access.require_admin()
    s = get_site(sid)
    p = _custom_path(s["domain"])
    return {"content": p.read_text() if p.exists() else "", "vhost": templates.vhost(s)}


@router.put("/{sid}/nginx")
def put_custom_nginx(sid: int, body: TextIn):
    access.require_admin()  # raw nginx directives = root-level power
    s = get_site(sid)
    if len(body.content) > 100_000:
        bad("Too large")
    p = _custom_path(s["domain"])
    old = p.read_text() if p.exists() else ""
    write_file(p, body.content if body.content.endswith("\n") else body.content + "\n")
    nginx_apply(lambda: write_file(p, old))
    db.log("site.nginx", s["domain"])
    return {"ok": True}


@router.get("/{sid}/logs")
def site_logs(sid: int, kind: str = "error", lines: int = 200):
    s = get_site(sid)
    files = {"error": "error.log", "access": "access.log", "php": "php-error.log"}
    if kind not in files:
        bad("Unknown log")
    return {"content": tail(site_home(s) / "logs" / files[kind], min(max(lines, 10), 2000))}


@router.delete("/{sid}")
def delete_site(sid: int, delete_files: bool = True, delete_dbs: bool = True):
    access.require_admin()
    s = get_site(sid)
    destroy(s, delete_files=delete_files, delete_dbs=delete_dbs)
    db.x("DELETE FROM crons WHERE site_id=?", (sid,))
    db.x("DELETE FROM sites WHERE id=?", (sid,))
    db.log("site.delete", s["domain"])
    return {"ok": True}

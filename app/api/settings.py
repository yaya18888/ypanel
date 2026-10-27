"""Panel settings + admin password."""
import os
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import auth, config, db, remote
from ..system import bad, detect_ip, php_versions, which, write_file

router = APIRouter(prefix="/api/settings", tags=["settings"])


def write_cron_d():
    h = db.setting("backup_hour")
    if h == "off":
        config.CRON_D.unlink(missing_ok=True)
        return
    write_file(config.CRON_D,
               "SHELL=/bin/sh\nPATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
               f"17 {int(h)} * * * root /usr/local/bin/ypanel backup-all >> /var/log/ypanel-backup.log 2>&1\n")


def write_cf_ini(token):
    if token:
        write_file(config.CF_INI, f"dns_cloudflare_api_token = {token}\n", mode=0o600)
    else:
        config.CF_INI.unlink(missing_ok=True)


@router.get("")
def get_settings():
    return {
        "admin_email": db.setting("admin_email"),
        "server_ip": db.setting("server_ip"),
        "detected_ip": detect_ip(),
        "cf_token_set": bool(db.setting("cf_token")),
        "backup_keep": db.setting("backup_keep"),
        "backup_exclude": db.setting("backup_exclude"),
        "backup_hour": db.setting("backup_hour"),
        "default_php": db.setting("default_php"),
        "php_versions": php_versions(),
        "version": config.VERSION,
        "remote": {
            "enabled": db.setting("remote_enabled") == "1",
            "provider": db.setting("s3_provider"),
            "endpoint": db.setting("s3_endpoint"),
            "region": db.setting("s3_region"),
            "bucket": db.setting("s3_bucket"),
            "access_key": db.setting("s3_access_key"),
            "secret_set": bool(db.setting("s3_secret")),
            "prefix": db.setting("s3_prefix"),
            "keep_days": db.setting("remote_keep_days"),
            "rclone": which("rclone"),
        },
    }


class RemoteIn(BaseModel):
    enabled: bool = False
    provider: str = "Cloudflare"
    endpoint: str = ""
    region: str = "auto"
    bucket: str = ""
    access_key: str = ""
    secret: str = ""          # empty = keep current
    prefix: str = "ypanel"
    keep_days: int = 30


@router.put("/remote")
def put_remote(b: RemoteIn):
    if b.provider not in remote.PROVIDERS:
        bad(f"Provider must be one of {', '.join(remote.PROVIDERS)}")
    if b.endpoint and not re.match(r"^https://[\w.-]+(:\d+)?/?$", b.endpoint.strip()):
        bad("Endpoint must look like https://<account>.r2.cloudflarestorage.com")
    if b.provider in ("Cloudflare", "Other") and not b.endpoint.strip():
        bad("Endpoint is required for this provider")
    if b.region and not re.match(r"^[a-z0-9-]{1,30}$", b.region):
        bad("Invalid region")
    if not re.match(r"^[a-z0-9][a-z0-9.-]{1,62}$", b.bucket.strip()):
        bad("Invalid bucket name")
    if not re.match(r"^[A-Za-z0-9/_.-]{0,100}$", b.prefix.strip()) or ".." in b.prefix:
        bad("Invalid path prefix")
    if not re.match(r"^[A-Za-z0-9/+=_-]{8,128}$", b.access_key.strip()):
        bad("Invalid access key")
    if b.secret and not re.match(r"^[A-Za-z0-9/+=_-]{8,128}$", b.secret.strip()):
        bad("Invalid secret key")
    if not b.secret and not db.setting("s3_secret"):
        bad("Secret key is required")
    if not 0 <= b.keep_days <= 3650:
        bad("Remote retention: 0-3650 days (0 = keep forever)")
    for k, v in (("s3_provider", b.provider), ("s3_endpoint", b.endpoint.strip().rstrip("/")),
                 ("s3_region", b.region), ("s3_bucket", b.bucket.strip()), ("s3_access_key", b.access_key.strip()),
                 ("s3_prefix", b.prefix.strip().strip("/")), ("remote_keep_days", b.keep_days),
                 ("remote_enabled", "1" if b.enabled else "0")):
        db.set_setting(k, v)
    if b.secret:
        db.set_setting("s3_secret", b.secret.strip())
    remote.write_conf()
    db.log("settings.remote", f"{b.provider} {b.bucket} ({'on' if b.enabled else 'off'})")
    return {"ok": True}


@router.post("/remote/test")
def test_remote():
    if not config.RCLONE_CONF.exists():
        bad("Save the remote settings first")
    if not which("rclone"):
        bad("rclone is not installed — re-run install.sh")
    return {"ok": True, "objects": remote.test()}


class SettingsIn(BaseModel):
    admin_email: Optional[str] = None
    server_ip: Optional[str] = None
    cf_token: Optional[str] = None
    backup_keep: Optional[int] = None
    backup_exclude: Optional[str] = None
    backup_hour: Optional[str] = None
    default_php: Optional[str] = None


@router.put("")
def put_settings(b: SettingsIn):
    if b.admin_email is not None:
        if b.admin_email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", b.admin_email):
            bad("Invalid email")
        db.set_setting("admin_email", b.admin_email.strip())
    if b.server_ip is not None:
        if b.server_ip and not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", b.server_ip.strip()):
            bad("Invalid IPv4")
        db.set_setting("server_ip", b.server_ip.strip())
    if b.cf_token is not None:
        tok = b.cf_token.strip()
        if tok and not re.match(r"^[A-Za-z0-9_-]{20,100}$", tok):
            bad("Token format looks wrong")
        db.set_setting("cf_token", tok)
        write_cf_ini(tok)
    if b.backup_keep is not None:
        if not 1 <= b.backup_keep <= 60:
            bad("Keep 1-60 backups")
        db.set_setting("backup_keep", b.backup_keep)
    if b.backup_exclude is not None:
        db.set_setting("backup_exclude", b.backup_exclude.strip()[:1000])
    if b.backup_hour is not None:
        if b.backup_hour != "off" and not (b.backup_hour.isdigit() and 0 <= int(b.backup_hour) <= 23):
            bad("Backup hour: 0-23 or off")
        db.set_setting("backup_hour", b.backup_hour)
        write_cron_d()
    if b.default_php is not None:
        if b.default_php and b.default_php not in php_versions():
            bad("PHP version not installed")
        db.set_setting("default_php", b.default_php)
    db.log("settings.update", ", ".join(k for k, v in b.model_dump().items() if v is not None and k != "cf_token")
           + (", cf_token" if b.cf_token is not None else ""))
    return {"ok": True}

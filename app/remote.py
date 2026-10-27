"""Remote backup storage via rclone (S3-compatible: Cloudflare R2, AWS S3, Backblaze B2, Wasabi, MinIO…)."""
import json
import os
import re

from . import config, db
from .system import bad, run, run_raw, which, write_file

PROVIDERS = ("Cloudflare", "AWS", "Wasabi", "Other")
REMOTE = "ypremote"


def enabled():
    return db.setting("remote_enabled") == "1" and config.RCLONE_CONF.exists() and which("rclone")


def write_conf():
    s = {k: db.setting(k) for k in ("s3_provider", "s3_endpoint", "s3_region", "s3_access_key", "s3_secret")}
    if not (s["s3_access_key"] and s["s3_secret"]):
        config.RCLONE_CONF.unlink(missing_ok=True)
        return
    lines = [f"[{REMOTE}]", "type = s3", f"provider = {s['s3_provider'] or 'Other'}",
             f"access_key_id = {s['s3_access_key']}", f"secret_access_key = {s['s3_secret']}",
             "no_check_bucket = true", "acl = private"]
    if s["s3_endpoint"]:
        lines.append(f"endpoint = {s['s3_endpoint']}")
    if s["s3_region"]:
        lines.append(f"region = {s['s3_region']}")
    write_file(config.RCLONE_CONF, "\n".join(lines) + "\n", mode=0o600)


def path(*parts):
    segs = [db.setting("s3_bucket"), db.setting("s3_prefix").strip("/")] + list(parts)
    return f"{REMOTE}:" + "/".join(p.strip("/") for p in segs if p)


def rclone(*args, timeout=6 * 3600, check=True):
    cmd = ["rclone", "--config", str(config.RCLONE_CONF), "--retries", "3", *args]
    if check:
        return run(cmd, timeout=timeout)
    return run_raw(cmd, timeout=timeout)


def upload(file, domain):
    rclone("copyto", file, path(domain, os.path.basename(file)), "--s3-no-check-bucket")
    try:
        days = int(db.setting("remote_keep_days") or 0)
    except ValueError:
        days = 0
    if days > 0:
        rclone("delete", path(domain), "--min-age", f"{days}d", check=False)


def listing(domain):
    code, out, _ = rclone("lsjson", path(domain), "--files-only", timeout=120, check=False)
    if code != 0:
        if "directory not found" in out.lower() or "not found" in out.lower():
            return []
        bad("rclone: " + out[-500:])
    items = [{"name": i["Name"], "size": i["Size"], "mtime": i.get("ModTime", "")} for i in json.loads(out or "[]")]
    return sorted(items, key=lambda i: i["name"], reverse=True)


def download(domain, name, dest):
    if not re.match(r"^[a-z0-9.-]+-\d{8}-\d{6}\.tar\.gz$", name):
        bad("Invalid backup name")
    rclone("copyto", path(domain, name), dest)


def test():
    code, out, _ = rclone("lsf", path(), "--max-depth", "1", timeout=60, check=False)
    if code != 0:
        bad("Connection failed:\n" + out[-800:])
    return len([x for x in out.splitlines() if x.strip()])

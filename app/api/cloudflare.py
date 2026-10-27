"""Cloudflare: DNS records, zone settings, cache purge. Token perms: Zone.Zone Read, Zone.DNS Edit (+ Zone Settings Edit, Cache Purge for those tabs)."""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from .. import db
from ..system import bad, detect_ip
from .sites import get_site

router = APIRouter(prefix="/api/cloudflare", tags=["cloudflare"])
API = "https://api.cloudflare.com/client/v4"


def cf(method, path, body=None, token=None):
    token = token or db.setting("cf_token")
    if not token:
        bad("Cloudflare API token is not set (Settings → Cloudflare)")
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            data = json.load(e)
        except Exception:  # noqa: BLE001
            bad(f"Cloudflare HTTP {e.code}")
    except urllib.error.URLError as e:
        bad(f"Cloudflare unreachable: {e.reason}")
    if not data.get("success"):
        bad("Cloudflare: " + "; ".join(m.get("message", "?") for m in data.get("errors", [])) or "error")
    return data


def find_zone(domain):
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        name = ".".join(parts[i:])
        r = cf("GET", "/zones?" + urllib.parse.urlencode({"name": name}))
        if r["result"]:
            return r["result"][0]
    bad(f"No Cloudflare zone found for {domain} in this account")


def upsert_a(zone_id, name, ip, proxied):
    q = urllib.parse.urlencode({"type": "A", "name": name})
    existing = cf("GET", f"/zones/{zone_id}/dns_records?{q}")["result"]
    rec = {"type": "A", "name": name, "content": ip, "ttl": 1, "proxied": bool(proxied)}
    if existing:
        cf("PUT", f"/zones/{zone_id}/dns_records/{existing[0]['id']}", rec)
        return "updated"
    cf("POST", f"/zones/{zone_id}/dns_records", rec)
    return "created"


def setup_site_dns(domain, www=True, proxied=True):
    ip = detect_ip()
    if not ip:
        bad("Server IP unknown — set it in Settings")
    zone = find_zone(domain)
    res = {domain: upsert_a(zone["id"], domain, ip, proxied)}
    if www:
        res[f"www.{domain}"] = upsert_a(zone["id"], f"www.{domain}", ip, proxied)
    db.log("cf.dns", f"{domain} → {ip} ({'proxied' if proxied else 'dns-only'})")
    return {"ip": ip, "zone": zone["name"], "records": res}


class TokenIn(BaseModel):
    token: str = ""


@router.post("/verify")
def verify(body: TokenIn):
    r = cf("GET", "/user/tokens/verify", token=body.token.strip() or None)
    zones = cf("GET", "/zones?per_page=50", token=body.token.strip() or None)["result"]
    return {"status": r["result"].get("status"), "zones": [z["name"] for z in zones]}


class DnsIn(BaseModel):
    site_id: int
    proxied: bool = True


@router.post("/dns")
def site_dns(body: DnsIn):
    s = get_site(body.site_id)
    return setup_site_dns(s["domain"], bool(s["www"]), body.proxied)


# ---------------- DNS manager ----------------
ID_RE = re.compile(r"^[a-f0-9]{32}$")
TYPES = ("A", "AAAA", "CNAME", "TXT", "MX", "NS", "SRV", "CAA")
PROXYABLE = ("A", "AAAA", "CNAME")
ZONE_SETTINGS = {
    "ssl": ("off", "flexible", "full", "strict"),
    "always_use_https": ("on", "off"),
    "development_mode": ("on", "off"),
    "min_tls_version": ("1.0", "1.1", "1.2", "1.3"),
    "browser_cache_ttl": None,  # int
    "security_level": ("essentially_off", "low", "medium", "high", "under_attack"),
}


def _zid(z):
    if not ID_RE.match(z):
        bad("Invalid id")
    return z


def _paged(path, max_pages=10):
    out, page = [], 1
    while page <= max_pages:
        sep = "&" if "?" in path else "?"
        r = cf("GET", f"{path}{sep}per_page=100&page={page}")
        out += r["result"]
        info = r.get("result_info") or {}
        if page >= (info.get("total_pages") or 1):
            break
        page += 1
    return out


@router.get("/zones")
def zones():
    return [{"id": z["id"], "name": z["name"], "status": z["status"],
             "plan": (z.get("plan") or {}).get("name", "")} for z in _paged("/zones", 5)]


@router.get("/zones/{zid}/records")
def records(zid: str):
    rows = _paged(f"/zones/{_zid(zid)}/dns_records")
    keys = ("id", "type", "name", "content", "proxied", "proxiable", "ttl", "priority", "comment")
    rows = [{k: r.get(k) for k in keys} for r in rows]
    return sorted(rows, key=lambda r: (r["name"].count("."), r["name"], r["type"]))


class RecordIn(BaseModel):
    type: str
    name: str
    content: str
    ttl: int = 1
    proxied: bool = False
    priority: Optional[int] = None
    comment: str = ""


def _record_body(b: RecordIn):
    t = b.type.upper()
    if t not in TYPES:
        bad(f"Type must be one of {', '.join(TYPES)}")
    if not b.name.strip() or not b.content.strip() or len(b.content) > 4096:
        bad("Name and content are required")
    if b.ttl != 1 and not 60 <= b.ttl <= 86400:
        bad("TTL: 1 (auto) or 60-86400")
    body = {"type": t, "name": b.name.strip(), "content": b.content.strip(), "ttl": b.ttl,
            "proxied": bool(b.proxied) if t in PROXYABLE else False}
    if t == "MX":
        body["priority"] = b.priority if b.priority is not None else 10
    if b.comment:
        body["comment"] = b.comment[:100]
    return body


@router.post("/zones/{zid}/records")
def create_record(zid: str, b: RecordIn):
    body = _record_body(b)
    r = cf("POST", f"/zones/{_zid(zid)}/dns_records", body)
    db.log("cf.record.create", f"{body['type']} {body['name']}")
    return r["result"]


@router.put("/zones/{zid}/records/{rid}")
def update_record(zid: str, rid: str, b: RecordIn):
    body = _record_body(b)
    r = cf("PUT", f"/zones/{_zid(zid)}/dns_records/{_zid(rid)}", body)
    db.log("cf.record.update", f"{body['type']} {body['name']}")
    return r["result"]


@router.delete("/zones/{zid}/records/{rid}")
def delete_record(zid: str, rid: str):
    cf("DELETE", f"/zones/{_zid(zid)}/dns_records/{_zid(rid)}")
    db.log("cf.record.delete", rid)
    return {"ok": True}


class PurgeIn(BaseModel):
    everything: bool = False
    files: List[str] = []


@router.post("/zones/{zid}/purge")
def purge(zid: str, b: PurgeIn):
    if b.everything:
        body = {"purge_everything": True}
    else:
        files = [f.strip() for f in b.files if f.strip()]
        if not files or len(files) > 30 or any(not f.startswith(("http://", "https://")) for f in files):
            bad("Give 1-30 full URLs, or purge everything")
        body = {"files": files}
    cf("POST", f"/zones/{_zid(zid)}/purge_cache", body)
    db.log("cf.purge", "everything" if b.everything else f"{len(body['files'])} url(s)")
    return {"ok": True}


@router.get("/zones/{zid}/settings")
def zone_settings(zid: str):
    rows = cf("GET", f"/zones/{_zid(zid)}/settings")["result"]
    return {r["id"]: r["value"] for r in rows if r["id"] in ZONE_SETTINGS}


class SettingIn(BaseModel):
    value: str


@router.patch("/zones/{zid}/settings/{key}")
def set_zone_setting(zid: str, key: str, b: SettingIn):
    if key not in ZONE_SETTINGS:
        bad("Setting not allowed")
    allowed = ZONE_SETTINGS[key]
    value = b.value
    if allowed is None:
        if not value.isdigit():
            bad("Number expected")
        value = int(value)
    elif value not in allowed:
        bad(f"Value must be one of {', '.join(allowed)}")
    cf("PATCH", f"/zones/{_zid(zid)}/settings/{key}", {"value": value})
    db.log("cf.setting", f"{key}={value}")
    return {"ok": True}


# ---------------- helpers used by the mail module ----------------
def cf_upsert(zone_id, rtype, name, content, match=None, priority=None, proxied=False):
    """Create or update one record. `match(record)` picks which existing record to replace."""
    q = urllib.parse.urlencode({"type": rtype, "name": name})
    existing = cf("GET", f"/zones/{zone_id}/dns_records?{q}")["result"]
    target = next((r for r in existing if (match(r) if match else True)), None)
    body = {"type": rtype, "name": name, "content": content, "ttl": 1, "proxied": bool(proxied)}
    if priority is not None:
        body["priority"] = priority
    if target:
        if target["content"].rstrip(".") == content.rstrip(".") and target.get("priority") == priority:
            return "unchanged"
        cf("PUT", f"/zones/{zone_id}/dns_records/{target['id']}", body)
        return "updated"
    cf("POST", f"/zones/{zone_id}/dns_records", body)
    return "created"

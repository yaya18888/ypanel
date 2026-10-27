"""Mail server: install, domains (+DKIM/DNS), mailboxes, aliases/forwarders, queue, TLS, health checks."""
import json
import re
import shutil
import socket
import subprocess
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config, db, mailsrv
from ..system import CmdError, DOMAIN_RE, bad, detect_ip, php_versions, run, run_raw, tail, which, write_file
from .sites import cert_info

router = APIRouter(prefix="/api/mail", tags=["mail"])
LOCAL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._+-]{0,62}[a-z0-9])?$")
EMAIL_RE = re.compile(r"^[^@\s,]+@[a-z0-9.-]+\.[a-z]{2,}$", re.I)
SERVICES = ("postfix", "dovecot", "rspamd", "redis-server")


def _need_installed():
    if not mailsrv.installed():
        bad("Mail server is not installed yet")


def _domain(did):
    d = db.q1("SELECT * FROM mail_domains WHERE id=?", (did,))
    if not d:
        raise HTTPException(404, "Mail domain not found")
    return d


# ---------------- status / install ----------------
@router.get("/status")
def status():
    st = config.MAIL_STATUS.read_text().strip() if config.MAIL_STATUS.exists() else ""
    host = db.setting("mail_hostname")
    services = {}
    if mailsrv.installed():
        for s in SERVICES:
            _, out, _ = run_raw(["systemctl", "is-active", s])
            out = out.strip()
            services[s] = out if out and len(out) < 20 and "\n" not in out else "unknown"
    return {"installed": bool(mailsrv.installed()), "setup": st, "hostname": host,
            "log": tail(config.MAIL_LOG, 60) if st in ("running", "failed") else "",
            "services": services, "cert": cert_info(host) if host else None,
            "webmail": (config.ROUNDCUBE / "public_html").exists(),
            "counts": {"domains": db.q1("SELECT COUNT(*) n FROM mail_domains")["n"],
                       "mailboxes": db.q1("SELECT COUNT(*) n FROM mailboxes")["n"],
                       "aliases": db.q1("SELECT COUNT(*) n FROM mail_aliases")["n"]}}


class InstallIn(BaseModel):
    hostname: str


@router.post("/install")
def install(b: InstallIn):
    host = b.hostname.strip().lower()
    if not DOMAIN_RE.match(host) or host.count(".") < 2:
        bad("Use a full hostname like mail.example.com")
    if config.MAIL_STATUS.exists() and config.MAIL_STATUS.read_text().strip() == "running":
        bad("Installation already running")
    script = config.SCRIPTS / "mail-setup.sh"
    if not script.exists():
        bad("mail-setup.sh missing — re-run install.sh (v3)")
    db.set_setting("mail_hostname", host)
    write_file(config.MAIL_STATUS, "running\n")
    log = open(config.MAIL_LOG, "a")
    log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} mail setup for {host} =====\n")
    log.flush()
    subprocess.Popen(["bash", str(script), host, str(config.BASE)], stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    db.log("mail.install", host)
    return {"ok": True}


@router.get("/checks")
def checks():
    ip, host = detect_ip(), db.setting("mail_hostname")
    out = {"ip": ip, "hostname": host, "port25": None, "port25_banner": "", "ptr": None, "ptr_ok": False,
           "host_a": None}
    try:
        with socket.create_connection(("gmail-smtp-in.l.google.com", 25), timeout=6) as s:
            s.settimeout(6)
            out["port25_banner"] = s.recv(200).decode(errors="replace").strip()[:120]
            out["port25"] = True
    except OSError as e:
        out["port25"] = False
        out["port25_banner"] = str(e)[:120]
    try:
        out["ptr"] = socket.gethostbyaddr(ip)[0] if ip else None
        out["ptr_ok"] = bool(host) and out["ptr"] == host
    except OSError:
        out["ptr"] = None
    try:
        out["host_a"] = socket.gethostbyname(host) if host else None
    except OSError:
        out["host_a"] = None
    return out


def _php_sock():
    v = db.setting("default_php")
    vs = php_versions()
    v = v if v in vs else (vs[-1] if vs else "8.3")
    return f"/run/php/php{v}-fpm.sock"


def _webmail_vhost(host):
    rc = config.ROUNDCUBE / "public_html"
    return f"""# Managed by YPanel — webmail for {host}
server {{
    listen 80;
    server_name {host};
    include snippets/ypanel-acme.conf;
    location / {{ return 301 https://$host$request_uri; }}
}}
server {{
    listen 443 ssl http2;
    server_name {host};
    ssl_certificate {config.LE_LIVE}/{host}/fullchain.pem;
    ssl_certificate_key {config.LE_LIVE}/{host}/privkey.pem;
    include snippets/ypanel-ssl.conf;
    root {rc};
    index index.php;
    client_max_body_size 50m;
    location ~ /\\. {{ deny all; }}
    location ~ \\.php$ {{
        try_files $uri =404;
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
        fastcgi_pass unix:{_php_sock()};
    }}
}}
"""


@router.post("/tls")
def issue_tls():
    _need_installed()
    host = db.setting("mail_hostname")
    email = db.setting("admin_email")
    if not email:
        bad("Set the admin email in Settings first")
    try:
        run(["certbot", "certonly", "--non-interactive", "--agree-tos", "-m", email, "--cert-name", host,
             "--key-type", "ecdsa", "--keep-until-expiring", "--webroot", "-w", str(config.ACME_ROOT),
             "-d", host, "--deploy-hook", "systemctl reload postfix dovecot nginx"], timeout=600)
    except CmdError as e:
        bad("Certbot failed — the A record of the mail host must point to this server (grey cloud):\n"
            + e.out[-1200:])
    live = config.LE_LIVE / host
    run(["postconf", "-e", f"smtpd_tls_cert_file={live}/fullchain.pem", f"smtpd_tls_key_file={live}/privkey.pem"])
    write_file("/etc/dovecot/conf.d/99-ypanel-ssl.conf",
               f"# Managed by YPanel\nssl_cert = <{live}/fullchain.pem\nssl_key = <{live}/privkey.pem\n")
    run(["systemctl", "reload", "postfix"], check=False)
    run(["systemctl", "reload", "dovecot"], check=False)
    if (config.ROUNDCUBE / "public_html").exists():
        vh = config.NGINX_SITES / "ypanel-webmail.conf"
        write_file(vh, _webmail_vhost(host))
        link = config.NGINX_ENABLED / vh.name
        if not link.exists():
            link.symlink_to(vh)
        code, out, _ = run_raw(["nginx", "-t"])
        if code != 0:
            link.unlink(missing_ok=True)
            bad("nginx test failed for webmail vhost:\n" + out[-600:])
        run(["systemctl", "reload", "nginx"])
    db.log("mail.tls", host)
    return {"ok": True, "cert": cert_info(host)}


# ---------------- domains ----------------
@router.get("/domains")
def list_domains():
    rows = db.q("SELECT d.*, (SELECT COUNT(*) FROM mailboxes m WHERE m.domain_id=d.id) boxes, "
                "(SELECT COUNT(*) FROM mail_aliases a WHERE a.domain_id=d.id) aliases FROM mail_domains d "
                "ORDER BY domain")
    for r in rows:
        r["dkim"] = mailsrv.dkim_key(r["domain"]).exists()
    return rows


class DomainIn(BaseModel):
    domain: str
    site_id: Optional[int] = None


@router.post("/domains")
def add_domain(b: DomainIn):
    _need_installed()
    d = b.domain.strip().lower()
    if d.startswith("www."):
        d = d[4:]
    if not DOMAIN_RE.match(d):
        bad("Invalid domain")
    if db.q1("SELECT id FROM mail_domains WHERE domain=?", (d,)):
        bad("Domain already added")
    mailsrv.dkim_generate(d)
    did = db.x("INSERT INTO mail_domains(domain,site_id,created) VALUES(?,?,?)", (d, b.site_id, int(time.time())))
    mailsrv.sync()
    db.log("mail.domain.add", d)
    return {"id": did, "domain": d}


@router.get("/domains/{did}/dns")
def domain_dns(did: int, check: bool = False):
    d = _domain(did)
    recs = mailsrv.dns_records(d["domain"])
    if check:
        for r in recs:
            r["ok"] = mailsrv.check_record(r)
    return recs


@router.post("/domains/{did}/dns/cloudflare")
def domain_dns_cloudflare(did: int):
    from .cloudflare import cf_upsert, find_zone
    d = _domain(did)
    zone = find_zone(d["domain"])
    res = []
    for r in mailsrv.dns_records(d["domain"]):
        if not r["content"]:
            continue
        name, t = r["name"], r["type"]
        if t == "A":
            if not (name == zone["name"] or name.endswith("." + zone["name"])):
                res.append({"record": f"A {name}", "result": "skipped (other zone)"})
                continue
            out = cf_upsert(zone["id"], "A", name, r["content"], proxied=False)
        elif t == "MX":
            out = cf_upsert(zone["id"], "MX", name, r["content"], priority=r["priority"],
                            match=lambda x, c=r["content"]: x["content"].rstrip(".") == c)
        elif r["content"].startswith("v=spf1"):
            out = cf_upsert(zone["id"], "TXT", name, r["content"],
                            match=lambda x: x["content"].strip('"').startswith("v=spf1"))
        else:
            out = cf_upsert(zone["id"], "TXT", name, r["content"])
        res.append({"record": f"{t} {name}", "result": out})
    db.log("mail.dns.cloudflare", d["domain"])
    return res


@router.delete("/domains/{did}")
def delete_domain(did: int, delete_mail: bool = False):
    d = _domain(did)
    db.x("DELETE FROM mailboxes WHERE domain_id=?", (did,))
    db.x("DELETE FROM mail_aliases WHERE domain_id=?", (did,))
    db.x("DELETE FROM mail_domains WHERE id=?", (did,))
    mailsrv.sync()
    mailsrv.dkim_key(d["domain"]).unlink(missing_ok=True)
    if delete_mail:
        shutil.rmtree(config.VMAIL / d["domain"], ignore_errors=True)
    db.log("mail.domain.delete", d["domain"])
    return {"ok": True}


# ---------------- mailboxes ----------------
def _usage(domain, local):
    p = config.VMAIL / domain / local
    if not p.exists():
        return 0
    out = run(["du", "-sb", str(p)], check=False, timeout=60)
    try:
        return int(out.split()[0])
    except (ValueError, IndexError):
        return 0


@router.get("/mailboxes")
def list_mailboxes(domain_id: Optional[int] = None, usage: bool = False):
    sql = ("SELECT m.id,m.domain_id,m.local,m.quota_mb,m.enabled,m.created,d.domain FROM mailboxes m "
           "JOIN mail_domains d ON d.id=m.domain_id")
    rows = db.q(sql + (" WHERE m.domain_id=?" if domain_id else "") + " ORDER BY d.domain, m.local",
                (domain_id,) if domain_id else ())
    for r in rows:
        r["address"] = f"{r['local']}@{r['domain']}"
        if usage:
            r["used"] = _usage(r["domain"], r["local"])
    return rows


class BoxIn(BaseModel):
    domain_id: int
    local: str
    password: str = ""
    quota_mb: int = 1024


@router.post("/mailboxes")
def add_mailbox(b: BoxIn):
    _need_installed()
    d = _domain(b.domain_id)
    local = b.local.strip().lower()
    if not LOCAL_RE.match(local):
        bad("Invalid mailbox name")
    addr = f"{local}@{d['domain']}"
    if db.q1("SELECT id FROM mailboxes WHERE domain_id=? AND local=?", (d["id"], local)) or \
            db.q1("SELECT id FROM mail_aliases WHERE source=?", (addr,)):
        bad("Address already exists (mailbox or forwarder)")
    if not 0 <= b.quota_mb <= 1024 * 1024:
        bad("Invalid quota")
    from ..system import gen_password
    pw = b.password or gen_password(16)
    mid = db.x("INSERT INTO mailboxes(domain_id,local,pw_hash,quota_mb,enabled,created) VALUES(?,?,?,?,1,?)",
               (d["id"], local, mailsrv.hash_password(pw), b.quota_mb, int(time.time())))
    mailsrv.sync()
    db.log("mail.box.add", addr)
    return {"id": mid, "address": addr, "password": pw}


class BoxUpd(BaseModel):
    password: Optional[str] = None
    quota_mb: Optional[int] = None
    enabled: Optional[bool] = None


@router.put("/mailboxes/{mid}")
def update_mailbox(mid: int, b: BoxUpd):
    m = db.q1("SELECT m.*, d.domain FROM mailboxes m JOIN mail_domains d ON d.id=m.domain_id WHERE m.id=?", (mid,))
    if not m:
        raise HTTPException(404, "Mailbox not found")
    out = {"ok": True}
    if b.password is not None:
        from ..system import gen_password
        pw = b.password or gen_password(16)
        db.x("UPDATE mailboxes SET pw_hash=? WHERE id=?", (mailsrv.hash_password(pw), mid))
        out["password"] = pw
    if b.quota_mb is not None:
        if not 0 <= b.quota_mb <= 1024 * 1024:
            bad("Invalid quota")
        db.x("UPDATE mailboxes SET quota_mb=? WHERE id=?", (b.quota_mb, mid))
    if b.enabled is not None:
        db.x("UPDATE mailboxes SET enabled=? WHERE id=?", (int(b.enabled), mid))
    mailsrv.sync()
    db.log("mail.box.update", f"{m['local']}@{m['domain']}")
    return out


@router.delete("/mailboxes/{mid}")
def delete_mailbox(mid: int, delete_mail: bool = True):
    m = db.q1("SELECT m.*, d.domain FROM mailboxes m JOIN mail_domains d ON d.id=m.domain_id WHERE m.id=?", (mid,))
    if not m:
        raise HTTPException(404, "Mailbox not found")
    db.x("DELETE FROM mailboxes WHERE id=?", (mid,))
    mailsrv.sync()
    if delete_mail:
        shutil.rmtree(config.VMAIL / m["domain"] / m["local"], ignore_errors=True)
    db.log("mail.box.delete", f"{m['local']}@{m['domain']}")
    return {"ok": True}


# ---------------- aliases / forwarders ----------------
@router.get("/aliases")
def list_aliases(domain_id: Optional[int] = None):
    sql = "SELECT a.*, d.domain FROM mail_aliases a JOIN mail_domains d ON d.id=a.domain_id"
    if domain_id:
        return db.q(sql + " WHERE a.domain_id=? ORDER BY a.source", (domain_id,))
    return db.q(sql + " ORDER BY a.source")


class AliasIn(BaseModel):
    domain_id: int
    local: str          # "" or "*" = catch-all
    destinations: str   # comma / newline separated addresses


def _dests(text):
    items = [x.strip().lower() for x in re.split(r"[,\s]+", text or "") if x.strip()]
    if not items or len(items) > 50:
        bad("Give 1-50 destination addresses")
    for x in items:
        if not EMAIL_RE.match(x):
            bad(f"Invalid address: {x}")
    return ",".join(dict.fromkeys(items))


@router.post("/aliases")
def add_alias(b: AliasIn):
    _need_installed()
    d = _domain(b.domain_id)
    local = b.local.strip().lower()
    if local in ("", "*", "@"):
        source = f"@{d['domain']}"
    else:
        if not LOCAL_RE.match(local):
            bad("Invalid alias name")
        source = f"{local}@{d['domain']}"
        if db.q1("SELECT id FROM mailboxes WHERE domain_id=? AND local=?", (d["id"], local)):
            bad("A mailbox with this address exists — delete it or forward from the mailbox instead")
    if db.q1("SELECT id FROM mail_aliases WHERE source=?", (source,)):
        bad("This forwarder already exists")
    aid = db.x("INSERT INTO mail_aliases(domain_id,source,destinations,created) VALUES(?,?,?,?)",
               (d["id"], source, _dests(b.destinations), int(time.time())))
    mailsrv.sync()
    db.log("mail.alias.add", source)
    return {"id": aid, "source": source}


class AliasUpd(BaseModel):
    destinations: str


@router.put("/aliases/{aid}")
def update_alias(aid: int, b: AliasUpd):
    a = db.q1("SELECT * FROM mail_aliases WHERE id=?", (aid,))
    if not a:
        raise HTTPException(404, "Forwarder not found")
    db.x("UPDATE mail_aliases SET destinations=? WHERE id=?", (_dests(b.destinations), aid))
    mailsrv.sync()
    return {"ok": True}


@router.delete("/aliases/{aid}")
def delete_alias(aid: int):
    a = db.q1("SELECT * FROM mail_aliases WHERE id=?", (aid,))
    if not a:
        raise HTTPException(404, "Forwarder not found")
    db.x("DELETE FROM mail_aliases WHERE id=?", (aid,))
    mailsrv.sync()
    db.log("mail.alias.delete", a["source"])
    return {"ok": True}


# ---------------- queue ----------------
@router.get("/queue")
def queue():
    _need_installed()
    out = run(["postqueue", "-j"], check=False)
    items = []
    for line in out.splitlines():
        try:
            j = json.loads(line)
        except ValueError:
            continue
        items.append({"id": j.get("queue_id"), "queue": j.get("queue_name"), "time": j.get("arrival_time"),
                      "size": j.get("message_size"), "sender": j.get("sender"),
                      "recipients": [{"address": r.get("address"), "reason": (r.get("delay_reason") or "")[:300]}
                                     for r in j.get("recipients", [])]})
    return items


@router.post("/queue/flush")
def queue_flush():
    _need_installed()
    run(["postqueue", "-f"])
    return {"ok": True}


@router.delete("/queue/{qid}")
def queue_delete(qid: str):
    _need_installed()
    if not re.match(r"^[A-Za-z0-9]{6,20}$", qid) and qid != "ALL":
        bad("Invalid queue id")
    run(["postsuper", "-d", qid])
    db.log("mail.queue.delete", qid)
    return {"ok": True}

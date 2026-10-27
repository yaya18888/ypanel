"""Mail server state → Postfix hash maps + Dovecot passwd-file, DKIM keys, DNS record set."""
import base64
import shutil
import subprocess

from . import config, db
from .system import bad, detect_ip, run, which, write_file


def installed():
    return (config.POSTFIX_MAPS.is_dir() and config.DOVECOT_USERS.exists()
            and which("postmap") and which("doveadm"))


def hash_password(pw):
    if len(pw) < 10 or "\n" in pw or ":" in pw:
        bad("Mailbox password: at least 10 characters, no ':'")
    out = run(["openssl", "passwd", "-6", "-stdin"], input=pw + "\n").strip()
    if not out.startswith("$6$"):
        bad("Could not hash password")
    return "{SHA512-CRYPT}" + out


def _rows():
    doms = db.q("SELECT * FROM mail_domains ORDER BY domain")
    mbs = db.q("SELECT m.*, d.domain FROM mailboxes m JOIN mail_domains d ON d.id=m.domain_id "
               "WHERE m.enabled=1 ORDER BY d.domain, m.local")
    als = db.q("SELECT a.*, d.domain FROM mail_aliases a JOIN mail_domains d ON d.id=a.domain_id ORDER BY a.source")
    return doms, mbs, als


def _write_map(name, lines):
    path = config.POSTFIX_MAPS / name
    write_file(path, "# Managed by YPanel — do not edit\n" + "".join(f"{l}\n" for l in lines), mode=0o644)
    run(["postmap", f"hash:{path}"])


def sync():
    """Regenerate every map from the panel DB. Postfix & Dovecot pick up changes without reload."""
    if not installed():
        return
    doms, mbs, als = _rows()
    boxes = {f"{m['local']}@{m['domain']}" for m in mbs}
    _write_map("domains", [f"{d['domain']} OK" for d in doms])
    _write_map("mailboxes", [f"{m['local']}@{m['domain']} {m['domain']}/{m['local']}/" for m in mbs])
    # a catch-all (@domain) in virtual_alias_maps would swallow real mailboxes too —
    # add identity entries so existing mailboxes keep their own mail
    catch_doms = {a["source"][1:] for a in als if a["source"].startswith("@")}
    alias_src = {a["source"] for a in als}
    identity = [f"{b} {b}" for b in sorted(boxes) if b.split("@")[1] in catch_doms and b not in alias_src]
    _write_map("aliases", [f"{a['source']} {a['destinations']}" for a in als] + identity)
    # who may send as which address (submission: reject_sender_login_mismatch)
    senders = {b: {b} for b in boxes}
    for a in als:
        local_dests = {x.strip() for x in a["destinations"].split(",") if x.strip() in boxes}
        if local_dests and not a["source"].startswith("@"):
            senders.setdefault(a["source"], set()).update(local_dests)
    _write_map("senders", [f"{k} {','.join(sorted(v))}" for k, v in sorted(senders.items())])
    lines = []
    for m in mbs:
        quota = f"userdb_quota_rule=*:storage={int(m['quota_mb'])}M" if int(m["quota_mb"] or 0) > 0 else ""
        lines.append(f"{m['local']}@{m['domain']}:{m['pw_hash']}::::::{quota}")
    write_file(config.DOVECOT_USERS, "".join(f"{l}\n" for l in lines), mode=0o640)
    try:
        shutil.chown(config.DOVECOT_USERS, "root", "dovecot")
    except (LookupError, OSError):
        pass


# ---------- DKIM ----------
def dkim_key(domain):
    return config.DKIM_DIR / f"{domain}.{config.DKIM_SELECTOR}.key"


def dkim_generate(domain):
    key = dkim_key(domain)
    if key.exists():
        return
    config.DKIM_DIR.mkdir(parents=True, exist_ok=True)
    run(["openssl", "genrsa", "-out", str(key), "2048"])
    key.chmod(0o640)
    try:
        shutil.chown(key, "_rspamd", "_rspamd")
    except (LookupError, OSError):
        pass


def dkim_txt(domain):
    key = dkim_key(domain)
    if not key.exists():
        return ""
    der = subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-outform", "DER"],
                         capture_output=True, timeout=30).stdout
    return f"v=DKIM1; k=rsa; p={base64.b64encode(der).decode()}"


# ---------- DNS ----------
def dns_records(domain):
    host = db.setting("mail_hostname")
    ip = detect_ip()
    recs = [
        {"type": "MX", "name": domain, "content": host, "priority": 10, "why": "Receive mail for the domain"},
        {"type": "TXT", "name": domain, "content": f"v=spf1 mx a ip4:{ip} ~all", "why": "SPF — allowed senders"},
        {"type": "TXT", "name": f"{config.DKIM_SELECTOR}._domainkey.{domain}", "content": dkim_txt(domain),
         "why": "DKIM signature key"},
        {"type": "TXT", "name": f"_dmarc.{domain}", "content": "v=DMARC1; p=none; adkim=r; aspf=r",
         "why": "DMARC policy (tighten to p=quarantine once mail flows fine)"},
    ]
    if host and (host == domain or host.endswith("." + domain)):
        recs.insert(0, {"type": "A", "name": host, "content": ip, "why": "Mail server host (DNS only, grey cloud)"})
    return recs


def dig(rtype, name):
    if not which("dig"):
        return None
    out = run(["dig", "+short", "+time=3", "+tries=1", rtype, name, "@1.1.1.1"], check=False, timeout=15)
    return [l.strip().strip('"') for l in out.splitlines() if l.strip() and not l.startswith(";")]


def check_record(rec):
    got = dig(rec["type"], rec["name"])
    if got is None:
        return None
    want = rec["content"].rstrip(".").lower()
    if rec["type"] == "MX":
        return any(g.split()[-1].rstrip(".").lower() == want for g in got if g)
    if rec["type"] == "TXT":
        # dig splits long TXT into "chunk" "chunk" — join them back
        vals = ["".join(g.split('" "')).replace(" ", "").lower() for g in got]
        if want.startswith("v=spf1"):
            ip = want.split("ip4:")[1].split()[0] if "ip4:" in want else ""
            return any(v.startswith("v=spf1") and ((ip and ip in v) or "mx" in v) for v in vals)
        if want.startswith("v=dmarc1"):
            return any(v.startswith("v=dmarc1") for v in vals)
        return want.replace(" ", "") in vals
    return want in [g.lower() for g in got]

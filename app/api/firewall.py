"""Firewall (ufw) + fail2ban management."""
import ipaddress
import re

from fastapi import APIRouter
from pydantic import BaseModel

from .. import db
from ..system import bad, run, run_raw
from .sftp import ssh_port

router = APIRouter(prefix="/api/firewall", tags=["firewall"])
RULE_RE = re.compile(r"^\[\s*(\d+)\]\s+(.+?)\s{2,}(ALLOW|DENY|REJECT|LIMIT)\s*(IN|OUT|FWD)?\s+(.+?)\s*$")
PORT_RE = re.compile(r"^\d{1,5}(:\d{1,5})?$")
JAIL_RE = re.compile(r"^[\w.-]{1,40}$")


def parse_rules(text):
    rules = []
    for line in text.splitlines():
        m = RULE_RE.match(line.strip())
        if m:
            to = m.group(2).strip()
            rules.append({"num": int(m.group(1)), "to": to, "action": m.group(3), "dir": m.group(4) or "IN",
                          "from": m.group(5).strip(), "v6": "(v6)" in to or "(v6)" in m.group(5)})
    return rules


def _protected_ports():
    return {str(ssh_port()), db.setting("panel_port") or "8090", "80", "443"}


def _is_protected(rule):
    port = rule["to"].split("/")[0].split(" ")[0]
    return rule["action"] in ("ALLOW", "LIMIT") and rule["from"].startswith("Anywhere") \
        and port in _protected_ports()


def _ip(value):
    try:
        return str(ipaddress.ip_network(value.strip(), strict=False))
    except ValueError:
        bad("Invalid IP / CIDR")


def parse_jails():
    code, out, _ = run_raw(["fail2ban-client", "status"])
    if code != 0:
        return {}
    m = re.search(r"Jail list:\s*(.*)", out)
    jails = {}
    for j in [x.strip() for x in (m.group(1) if m else "").split(",") if x.strip()]:
        _, o, _ = run_raw(["fail2ban-client", "status", j])
        b = re.search(r"Banned IP list:\s*(.*)", o)
        t = re.search(r"Total banned:\s*(\d+)", o)
        jails[j] = {"banned": (b.group(1).split() if b else []), "total": int(t.group(1)) if t else 0}
    return jails


@router.get("")
def status():
    code, out, _ = run_raw(["ufw", "status", "numbered"])
    return {"active": code == 0 and "Status: active" in out,
            "available": code != 127,
            "rules": parse_rules(out),
            "protected": sorted(_protected_ports(), key=int),
            "jails": parse_jails()}


class RuleIn(BaseModel):
    action: str = "allow"
    port: str = ""
    proto: str = "tcp"
    source: str = ""


@router.post("/rules")
def add_rule(b: RuleIn):
    if b.action not in ("allow", "deny", "limit"):
        bad("Action must be allow, deny or limit")
    if b.proto not in ("tcp", "udp", "any"):
        bad("Protocol must be tcp, udp or any")
    port = b.port.strip()
    if port and not PORT_RE.match(port):
        bad("Port must be like 3306 or 6000:6100")
    if ":" in port and b.proto == "any":
        bad("Port ranges need tcp or udp")
    src = _ip(b.source) if b.source.strip() else "any"
    if not port and src == "any":
        bad("Give a port, a source IP, or both")
    cmd = ["ufw", b.action]
    if b.proto != "any" and port:
        cmd += ["proto", b.proto]
    cmd += ["from", src, "to", "any"]
    if port:
        cmd += ["port", port]
    out = run(cmd)
    db.log("firewall.add", " ".join(cmd[1:]))
    return {"ok": True, "output": out}


class IpIn(BaseModel):
    ip: str
    jail: str = ""


@router.post("/block")
def block_ip(b: IpIn):
    ip = _ip(b.ip)
    net = ipaddress.ip_network(ip)
    if any(net.overlaps(ipaddress.ip_network(x)) for x in ("127.0.0.0/8", "::1/128")):
        bad("Refusing to block localhost")
    out = run(["ufw", "prepend", "deny", "from", ip])
    db.log("firewall.block", ip)
    return {"ok": True, "output": out}


@router.delete("/rules/{num}")
def delete_rule(num: int):
    _, out, _ = run_raw(["ufw", "status", "numbered"])
    rule = next((r for r in parse_rules(out) if r["num"] == num), None)
    if not rule:
        bad("Rule not found", 404)
    if _is_protected(rule):
        bad("This rule keeps SSH / web / panel reachable — refusing to delete it")
    run(["ufw", "--force", "delete", str(num)])
    db.log("firewall.delete", f"#{num} {rule['to']} {rule['action']} {rule['from']}")
    return {"ok": True}


class ToggleIn(BaseModel):
    enabled: bool


@router.post("/toggle")
def toggle(b: ToggleIn):
    if b.enabled:
        for p in _protected_ports():
            run(["ufw", "allow", f"{p}/tcp"])
        run(["ufw", "--force", "enable"])
    else:
        run(["ufw", "disable"])
    db.log("firewall.toggle", "on" if b.enabled else "off")
    return {"ok": True}


@router.post("/unban")
def unban(b: IpIn):
    if not JAIL_RE.match(b.jail):
        bad("Invalid jail")
    ip = _ip(b.ip).split("/")[0]
    run(["fail2ban-client", "set", b.jail, "unbanip", ip])
    db.log("fail2ban.unban", f"{b.jail} {ip}")
    return {"ok": True}

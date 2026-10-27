"""Server monitoring & service control."""
import platform
import time
from pathlib import Path

import psutil
from fastapi import APIRouter
from pydantic import BaseModel

from .. import config, db
from ..system import bad, php_versions, run, run_raw

router = APIRouter(prefix="/api/server", tags=["server"])
ACTIONS = ("start", "stop", "restart", "reload")


def _services():
    return ["nginx", "mariadb", "cron", "fail2ban"] + [f"php{v}-fpm" for v in php_versions()]


def _os_name():
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return platform.platform()


@router.get("/stats")
def stats():
    vm, sw, du = psutil.virtual_memory(), psutil.swap_memory(), psutil.disk_usage("/")
    net = psutil.net_io_counters()
    return {
        "hostname": platform.node(),
        "os": _os_name(),
        "kernel": platform.release(),
        "panel": config.VERSION,
        "cpu": psutil.cpu_percent(interval=0.3),
        "cores": psutil.cpu_count(),
        "load": list(psutil.getloadavg()),
        "mem": {"total": vm.total, "used": vm.total - vm.available, "percent": vm.percent},
        "swap": {"total": sw.total, "used": sw.used, "percent": sw.percent},
        "disk": {"total": du.total, "used": du.used, "percent": du.percent},
        "net": {"sent": net.bytes_sent, "recv": net.bytes_recv},
        "uptime": int(time.time() - psutil.boot_time()),
        "sites": db.q1("SELECT COUNT(*) n FROM sites")["n"],
        "dbs": db.q1("SELECT COUNT(*) n FROM dbs")["n"],
    }


@router.get("/services")
def services():
    out = []
    for s in _services():
        _, state, _ = run_raw(["systemctl", "is-active", s])
        state = state.strip()
        out.append({"name": s, "state": state if state and len(state) < 20 and "\n" not in state else "unknown"})
    return out


@router.post("/services/{name}/{action}")
def service_action(name: str, action: str):
    if name not in _services() or action not in ACTIONS:
        bad("Not allowed")
    if name == "nginx" and action in ("restart", "reload"):
        code, out, _ = run_raw(["nginx", "-t"])
        if code != 0:
            bad("nginx config test failed:\n" + out[-600:])
    run(["systemctl", action, name], timeout=120)
    db.log("service", f"{action} {name}")
    return {"ok": True}


@router.get("/processes")
def processes():
    procs = []
    for p in psutil.process_iter(["pid", "name", "username", "memory_info", "cpu_percent"]):
        i = p.info
        if not i.get("memory_info"):
            continue
        procs.append({"pid": i["pid"], "name": i["name"], "user": i["username"],
                      "rss": i["memory_info"].rss, "cpu": i["cpu_percent"]})
    procs.sort(key=lambda x: x["rss"], reverse=True)
    return procs[:20]


@router.get("/php")
def php():
    return {"versions": php_versions(), "default": db.setting("default_php")}


@router.get("/activity")
def activity(limit: int = 100):
    return db.q("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 500),))


class KillIn(BaseModel):
    pid: int


@router.post("/kill")
def kill(body: KillIn):
    if body.pid <= 1:
        bad("Not allowed")
    try:
        psutil.Process(body.pid).terminate()
    except psutil.Error as e:
        bad(str(e))
    db.log("process.kill", str(body.pid))
    return {"ok": True}

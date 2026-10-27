"""YPanel — FastAPI app."""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, config, db
from .api import (backups, cloudflare, cron, databases, files, firewall, installers, mail, redis, security,
                  server, settings, sftp, sites, stats, users)
from .system import CmdError


@asynccontextmanager
async def lifespan(_app):
    db.init()
    yield


app = FastAPI(title="YPanel", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware("http")
async def guard(request: Request, call_next):
    # CSRF: state-changing API calls must carry a custom header (cross-site forms can't set it)
    if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("x-ypanel") != "1":
            return JSONResponse({"detail": "Missing X-YPanel header"}, status_code=403)
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.exception_handler(CmdError)
async def cmd_error(_req, exc: CmdError):
    return JSONResponse({"detail": f"Command failed ({exc.code}):\n{exc.out[-1200:]}"}, status_code=500)


app.include_router(auth.router)
any_user = [Depends(auth.require_auth)]
admin_only = [Depends(auth.require_admin)]
# site-scoped routers: access is enforced per site inside (get_site / filter_sites)
for r in (sites, databases, files, cron, backups, installers, security, sftp, redis, stats):
    app.include_router(r.router, dependencies=any_user)
# server-wide routers
for r in (server, cloudflare, settings, firewall, mail, users):
    app.include_router(r.router, dependencies=admin_only)

app.mount("/static", StaticFiles(directory=str(config.STATIC)), name="static")


@app.get("/")
def index():
    return FileResponse(str(config.STATIC / "index.html"), headers={"Cache-Control": "no-cache"})

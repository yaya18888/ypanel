"""Admin auth: bcrypt passwords, hashed session tokens, login throttling."""
import hashlib
import json
import secrets
import time

import bcrypt
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from . import access, config, db, totp

router = APIRouter(prefix="/api", tags=["auth"])

_fails = {}  # ip -> [count, first_ts]
MAX_FAILS, LOCK_SECONDS = 5, 900


def hash_pw(p):
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt(12)).decode()


def check_pw(p, h):
    try:
        return bcrypt.checkpw(p.encode(), h.encode())
    except ValueError:
        return False


def _sha(tok):
    return hashlib.sha256(tok.encode()).hexdigest()


def client_ip(req):
    return req.headers.get("x-real-ip") or (req.client.host if req.client else "?")


def session_for(request: Request):
    tok = request.cookies.get(config.COOKIE)
    if tok:
        s = db.q1("SELECT * FROM sessions WHERE token=? AND expires>?", (_sha(tok), int(time.time())))
        if s:
            a = db.q1("SELECT * FROM admins WHERE id=?", (s["admin_id"],))
            if a:
                return s, a
    raise HTTPException(401, "Not authenticated")


async def require_auth(request: Request):
    """async on purpose: sets the ContextVar in the request task so sync endpoints inherit it."""
    s, a = session_for(request)
    access.set_user(a)
    return s


async def require_admin(request: Request):
    s, a = session_for(request)
    access.set_user(a)
    if (a.get("role") or "admin") != "admin":
        raise HTTPException(403, "Admin only")
    return s


class LoginIn(BaseModel):
    username: str
    password: str
    code: str = ""


def _fail(ip, now, what):
    f = _fails.get(ip)
    if not f or now - f[1] >= LOCK_SECONDS:
        _fails[ip] = [1, now]
    else:
        f[0] += 1
    db.log("login.fail", f"{what} from {ip}")


def use_recovery(admin, code):
    hashes = json.loads(admin.get("recovery") or "[]")
    h = totp.hash_code(code)
    if h not in hashes:
        return False
    hashes.remove(h)
    db.x("UPDATE admins SET recovery=? WHERE id=?", (json.dumps(hashes), admin["id"]))
    db.log("2fa.recovery_used", f"{admin['username']} ({len(hashes)} left)")
    return True


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response):
    ip, now = client_ip(request), time.time()
    f = _fails.get(ip)
    if f and f[0] >= MAX_FAILS and now - f[1] < LOCK_SECONDS:
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
    admin = db.q1("SELECT * FROM admins WHERE username=?", (body.username.strip(),))
    if not admin or not check_pw(body.password, admin["pw_hash"]):
        _fail(ip, now, body.username[:40])
        raise HTTPException(401, "Invalid username or password")
    if admin.get("totp_enabled"):
        if not body.code.strip():
            raise HTTPException(401, "2FA_REQUIRED")
        step = totp.verify(admin["totp_secret"], body.code, admin.get("totp_last") or 0)
        if step:
            db.x("UPDATE admins SET totp_last=? WHERE id=?", (step, admin["id"]))
        elif not use_recovery(admin, body.code):
            _fail(ip, now, f"{admin['username']} (bad 2FA code)")
            raise HTTPException(401, "Invalid 2FA code")
    _fails.pop(ip, None)
    tok = secrets.token_urlsafe(32)
    db.x("DELETE FROM sessions WHERE expires<?", (int(now),))
    db.x("INSERT INTO sessions(token,admin_id,expires,ip) VALUES(?,?,?,?)",
         (_sha(tok), admin["id"], int(now) + config.SESSION_TTL, ip))
    response.set_cookie(config.COOKIE, tok, max_age=config.SESSION_TTL, httponly=True,
                        secure=config.SECURE_COOKIE, samesite="strict", path="/")
    db.log("login.ok", f"{admin['username']} from {ip}")
    return {"ok": True, "username": admin["username"], "role": admin.get("role") or "admin"}


@router.post("/logout")
def logout(request: Request, response: Response):
    tok = request.cookies.get(config.COOKIE)
    if tok:
        db.x("DELETE FROM sessions WHERE token=?", (_sha(tok),))
    response.delete_cookie(config.COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    _, a = session_for(request)
    return {"username": a["username"], "role": a.get("role") or "admin",
            "sites": json.loads(a.get("sites") or "[]")}


@router.get("/session-check")
def session_check(request: Request):
    """Used by nginx auth_request to protect /phpmyadmin/."""
    session_for(request)
    return Response(status_code=204)

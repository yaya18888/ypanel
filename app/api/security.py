"""Admin two-factor authentication (TOTP + one-time recovery codes)."""
import json
import platform

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import auth, db, totp
from ..system import bad

router = APIRouter(prefix="/api/security", tags=["security"])


def _admin(session):
    a = db.q1("SELECT * FROM admins WHERE id=?", (session["admin_id"],))
    if not a:
        bad("Admin not found", 404)
    return a


def _new_recovery(admin_id):
    codes = totp.recovery_codes()
    db.x("UPDATE admins SET recovery=? WHERE id=?", (json.dumps([totp.hash_code(c) for c in codes]), admin_id))
    return codes


@router.get("/2fa")
def status(session=Depends(auth.require_auth)):
    a = _admin(session)
    return {"enabled": bool(a["totp_enabled"]), "recovery_left": len(json.loads(a["recovery"] or "[]"))}


@router.post("/2fa/setup")
def setup(session=Depends(auth.require_auth)):
    a = _admin(session)
    if a["totp_enabled"]:
        bad("2FA is already enabled — disable it first")
    secret = totp.new_secret()
    db.x("UPDATE admins SET totp_pending=? WHERE id=?", (secret, a["id"]))
    return {"secret": secret, "uri": totp.uri(secret, f"{a['username']}@{platform.node()}")}


class CodeIn(BaseModel):
    code: str


@router.post("/2fa/enable")
def enable(body: CodeIn, session=Depends(auth.require_auth)):
    a = _admin(session)
    if not a["totp_pending"]:
        bad("Start the setup first")
    step = totp.verify(a["totp_pending"], body.code)
    if not step:
        bad("Wrong code — check the time on your phone and try again")
    db.x("UPDATE admins SET totp_secret=totp_pending, totp_pending='', totp_enabled=1, totp_last=? WHERE id=?",
         (step, a["id"]))
    codes = _new_recovery(a["id"])
    db.log("2fa.enable", a["username"])
    return {"recovery_codes": codes}


class PwIn(BaseModel):
    password: str


@router.post("/2fa/disable")
def disable(body: PwIn, session=Depends(auth.require_auth)):
    a = _admin(session)
    if not auth.check_pw(body.password, a["pw_hash"]):
        bad("Wrong password")
    db.x("UPDATE admins SET totp_secret='', totp_pending='', totp_enabled=0, totp_last=0, recovery='[]' "
         "WHERE id=?", (a["id"],))
    db.log("2fa.disable", a["username"])
    return {"ok": True}


@router.post("/2fa/recovery")
def regen_recovery(body: PwIn, session=Depends(auth.require_auth)):
    a = _admin(session)
    if not a["totp_enabled"]:
        bad("2FA is not enabled")
    if not auth.check_pw(body.password, a["pw_hash"]):
        bad("Wrong password")
    db.log("2fa.recovery_regen", a["username"])
    return {"recovery_codes": _new_recovery(a["id"])}


class PasswordIn(BaseModel):
    current: str
    new: str


@router.post("/password")
def change_password(b: PasswordIn, session=Depends(auth.require_auth)):
    a = db.q1("SELECT * FROM admins WHERE id=?", (session["admin_id"],))
    if not a or not auth.check_pw(b.current, a["pw_hash"]):
        bad("Current password is wrong")
    if len(b.new) < 10:
        bad("New password must be at least 10 characters")
    db.x("UPDATE admins SET pw_hash=? WHERE id=?", (auth.hash_pw(b.new), a["id"]))
    db.x("DELETE FROM sessions WHERE admin_id=? AND token<>?", (a["id"], session["token"]))
    db.log("admin.password", a["username"])
    return {"ok": True}

"""Panel users: admins (everything) and site users (only their assigned sites)."""
import json
import re
import time
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from .. import access, auth, db
from ..system import bad, gen_password

router = APIRouter(prefix="/api/users", tags=["users"])
USER_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")


def _row(a):
    return {"id": a["id"], "username": a["username"], "role": a.get("role") or "admin",
            "sites": json.loads(a.get("sites") or "[]"), "totp": bool(a.get("totp_enabled")), "created": a["created"]}


def _clean_sites(ids):
    valid = {r["id"] for r in db.q("SELECT id FROM sites")}
    return sorted({int(i) for i in ids if int(i) in valid})


def _admins_left(excluding):
    return db.q1("SELECT COUNT(*) n FROM admins WHERE COALESCE(role,'admin')='admin' AND id<>?", (excluding,))["n"]


@router.get("")
def list_users():
    rows = [_row(a) for a in db.q("SELECT * FROM admins ORDER BY username")]
    names = {s["id"]: s["domain"] for s in db.q("SELECT id, domain FROM sites")}
    for r in rows:
        r["domains"] = [names[i] for i in r["sites"] if i in names]
    return rows


class UserIn(BaseModel):
    username: str
    password: str = ""
    role: str = "user"
    sites: List[int] = []


@router.post("")
def create_user(b: UserIn):
    if not USER_RE.match(b.username):
        bad("Username: 3-32 chars, letters, digits, _ . -")
    if b.role not in ("admin", "user"):
        bad("Role must be admin or user")
    if db.q1("SELECT id FROM admins WHERE username=?", (b.username,)):
        bad("Username already exists")
    pw = b.password or gen_password(16)
    if len(pw) < 10:
        bad("Password must be at least 10 characters")
    db.x("INSERT INTO admins(username,pw_hash,created,role,sites) VALUES(?,?,?,?,?)",
         (b.username, auth.hash_pw(pw), int(time.time()), b.role, json.dumps(_clean_sites(b.sites))))
    db.log("user.create", f"{b.username} ({b.role})")
    return {"username": b.username, "password": pw}


class UserUpd(BaseModel):
    role: Optional[str] = None
    sites: Optional[List[int]] = None
    password: Optional[str] = None


@router.put("/{uid}")
def update_user(uid: int, b: UserUpd):
    a = db.q1("SELECT * FROM admins WHERE id=?", (uid,))
    if not a:
        bad("User not found", 404)
    out = {"ok": True}
    if b.role is not None:
        if b.role not in ("admin", "user"):
            bad("Role must be admin or user")
        if b.role == "user" and (a.get("role") or "admin") == "admin" and not _admins_left(uid):
            bad("Can't demote the last admin")
        db.x("UPDATE admins SET role=? WHERE id=?", (b.role, uid))
    if b.sites is not None:
        db.x("UPDATE admins SET sites=? WHERE id=?", (json.dumps(_clean_sites(b.sites)), uid))
    if b.password is not None:
        pw = b.password or gen_password(16)
        if len(pw) < 10:
            bad("Password must be at least 10 characters")
        db.x("UPDATE admins SET pw_hash=? WHERE id=?", (auth.hash_pw(pw), uid))
        db.x("DELETE FROM sessions WHERE admin_id=?", (uid,))
        out["password"] = pw
    db.log("user.update", a["username"])
    return out


@router.post("/{uid}/2fa-off")
def reset_2fa(uid: int):
    a = db.q1("SELECT * FROM admins WHERE id=?", (uid,))
    if not a:
        bad("User not found", 404)
    db.x("UPDATE admins SET totp_secret='', totp_pending='', totp_enabled=0, totp_last=0, recovery='[]' WHERE id=?",
         (uid,))
    db.log("user.2fa_reset", a["username"])
    return {"ok": True}


@router.delete("/{uid}")
def delete_user(uid: int):
    a = db.q1("SELECT * FROM admins WHERE id=?", (uid,))
    if not a:
        bad("User not found", 404)
    me = access.current()
    if me and me["id"] == uid:
        bad("You can't delete yourself")
    if (a.get("role") or "admin") == "admin" and not _admins_left(uid):
        bad("Can't delete the last admin")
    db.x("DELETE FROM sessions WHERE admin_id=?", (uid,))
    db.x("DELETE FROM admins WHERE id=?", (uid,))
    db.log("user.delete", a["username"])
    return {"ok": True}

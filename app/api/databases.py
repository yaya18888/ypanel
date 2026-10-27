"""MariaDB databases & users (cPanel-style prefix: <siteuser>_<name>)."""
import time

import pymysql
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import access, config, db
from ..system import NAME_RE, bad, gen_password
from .sites import get_site

router = APIRouter(prefix="/api/databases", tags=["databases"])
HOSTS = ("localhost", "127.0.0.1")


def mconn():
    try:
        return pymysql.connect(read_default_file=str(config.MYCNF), charset="utf8mb4", autocommit=True)
    except pymysql.MySQLError as e:
        raise HTTPException(500, f"MySQL connection failed: {e}")


def _grant_name(name):
    # '_' is a wildcard in GRANT ... ON `db`.* — escape it
    return name.replace("_", "\\_")


def create_database(site, suffix, password=None):
    suffix = (suffix or "").strip().lower()
    if not NAME_RE.match(suffix):
        bad("Database name: a-z, 0-9 and _ only (max 32)")
    name = f"{site['user']}_{suffix}"
    if len(name) > 64:
        bad("Database name too long")
    if db.q1("SELECT id FROM dbs WHERE name=?", (name,)):
        bad("Database already exists")
    password = password or gen_password(24)
    with mconn() as c:
        cur = c.cursor()
        cur.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s", (name,))
        if cur.fetchone():
            bad("Database already exists in MySQL")
        try:
            cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            for h in HOSTS:
                cur.execute(f"CREATE USER '{name}'@'{h}' IDENTIFIED BY %s", (password,))
                cur.execute(f"GRANT ALL PRIVILEGES ON `{_grant_name(name)}`.* TO '{name}'@'{h}'")
        except pymysql.MySQLError as e:
            _drop(cur, name)
            bad(f"MySQL error: {e}")
    db.x("INSERT INTO dbs(name,user,site_id,created) VALUES(?,?,?,?)",
         (name, name, site["id"], int(time.time())))
    db.log("db.create", name)
    return {"name": name, "user": name, "password": password, "host": "localhost"}


def _drop(cur, name):
    cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
    for h in HOSTS:
        cur.execute(f"DROP USER IF EXISTS '{name}'@'{h}'")


def drop_database(name):
    if not NAME_RE.match(name.split("_", 1)[-1]) or "`" in name or "'" in name:
        bad("Invalid name")
    with mconn() as c:
        _drop(c.cursor(), name)


@router.get("")
def list_dbs():
    rows = access.filter_sites(db.q("SELECT d.id,d.name,d.user,d.site_id,d.created,s.domain FROM dbs d "
                                    "LEFT JOIN sites s ON s.id=d.site_id ORDER BY d.name"))
    sizes = {}
    try:
        with mconn() as c:
            cur = c.cursor()
            cur.execute("SELECT table_schema, SUM(data_length+index_length) FROM information_schema.TABLES "
                        "GROUP BY table_schema")
            sizes = {r[0]: int(r[1] or 0) for r in cur.fetchall()}
    except HTTPException:
        pass
    for r in rows:
        r["size"] = sizes.get(r["name"], 0)
    return rows


class DbIn(BaseModel):
    site_id: int
    name: str
    password: str = ""


@router.post("")
def create_db(body: DbIn):
    site = get_site(body.site_id)
    if body.password and len(body.password) < 10:
        bad("Password must be at least 10 characters")
    return create_database(site, body.name, body.password or None)


class PwIn(BaseModel):
    password: str = ""


@router.put("/{did}/password")
def change_pw(did: int, body: PwIn):
    row = db.q1("SELECT * FROM dbs WHERE id=?", (did,))
    if not row or not access.can_site(row["site_id"]):
        raise HTTPException(404, "Database not found")
    pw = body.password or gen_password(24)
    if len(pw) < 10:
        bad("Password must be at least 10 characters")
    with mconn() as c:
        cur = c.cursor()
        for h in HOSTS:
            cur.execute(f"ALTER USER '{row['user']}'@'{h}' IDENTIFIED BY %s", (pw,))
    db.log("db.password", row["name"])
    return {"user": row["user"], "password": pw}


@router.delete("/{did}")
def delete_db(did: int):
    row = db.q1("SELECT * FROM dbs WHERE id=?", (did,))
    if not row or not access.can_site(row["site_id"]):
        raise HTTPException(404, "Database not found")
    drop_database(row["name"])
    db.x("DELETE FROM dbs WHERE id=?", (did,))
    db.log("db.delete", row["name"])
    return {"ok": True}

"""Panel state (SQLite)."""
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS admins(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL, created INTEGER);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY, admin_id INTEGER, expires INTEGER, ip TEXT);
CREATE TABLE IF NOT EXISTS sites(
  id INTEGER PRIMARY KEY, domain TEXT UNIQUE NOT NULL, user TEXT UNIQUE NOT NULL,
  php TEXT NOT NULL, www INTEGER DEFAULT 1, ssl INTEGER DEFAULT 0, ini TEXT DEFAULT '{}', created INTEGER);
CREATE TABLE IF NOT EXISTS dbs(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, user TEXT NOT NULL, site_id INTEGER, created INTEGER);
CREATE TABLE IF NOT EXISTS crons(
  id INTEGER PRIMARY KEY, site_id INTEGER, schedule TEXT, command TEXT, enabled INTEGER DEFAULT 1, created INTEGER);
CREATE TABLE IF NOT EXISTS backups(
  id INTEGER PRIMARY KEY, site_id INTEGER, file TEXT, size INTEGER DEFAULT 0, status TEXT,
  note TEXT DEFAULT '', created INTEGER);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY, ts INTEGER, action TEXT, detail TEXT);
"""

# v2 columns added to existing v1 databases
MIGRATIONS = [
    ("admins", "totp_secret", "TEXT DEFAULT ''"),
    ("admins", "totp_pending", "TEXT DEFAULT ''"),
    ("admins", "totp_enabled", "INTEGER DEFAULT 0"),
    ("admins", "totp_last", "INTEGER DEFAULT 0"),
    ("admins", "recovery", "TEXT DEFAULT '[]'"),
    ("sites", "sftp", "INTEGER DEFAULT 0"),
    ("backups", "remote", "TEXT DEFAULT ''"),
    # v3
    ("admins", "role", "TEXT DEFAULT 'admin'"),
    ("admins", "sites", "TEXT DEFAULT '[]'"),
    ("sites", "redis_user", "TEXT DEFAULT ''"),
    ("sites", "redis_pass", "TEXT DEFAULT ''"),
]

# v3 tables (mail)
SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS mail_domains(
  id INTEGER PRIMARY KEY, domain TEXT UNIQUE NOT NULL, site_id INTEGER, created INTEGER);
CREATE TABLE IF NOT EXISTS mailboxes(
  id INTEGER PRIMARY KEY, domain_id INTEGER NOT NULL, local TEXT NOT NULL, pw_hash TEXT NOT NULL,
  quota_mb INTEGER DEFAULT 1024, enabled INTEGER DEFAULT 1, created INTEGER, UNIQUE(domain_id, local));
CREATE TABLE IF NOT EXISTS mail_aliases(
  id INTEGER PRIMARY KEY, domain_id INTEGER NOT NULL, source TEXT UNIQUE NOT NULL, destinations TEXT NOT NULL,
  created INTEGER);
"""

DEFAULTS = {
    "admin_email": "",
    "server_ip": "",
    "cf_token": "",
    "backup_keep": "5",
    "backup_exclude": "",
    "backup_hour": "3",
    "default_php": "",
    "panel_port": "8090",
    "remote_enabled": "0",
    "s3_provider": "Cloudflare",
    "s3_endpoint": "",
    "s3_region": "auto",
    "s3_bucket": "",
    "s3_access_key": "",
    "s3_secret": "",
    "s3_prefix": "ypanel",
    "remote_keep_days": "30",
    "mail_hostname": "",
    "redis_maxmemory": "128mb",
}

_wlock = threading.Lock()


@contextmanager
def _conn():
    c = sqlite3.connect(str(config.DB_PATH), timeout=15)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    config.DATA.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        c.executescript(SCHEMA_V3)
        for table, col, decl in MIGRATIONS:
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
            if col not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def q(sql, args=()):
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def q1(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def x(sql, args=()):
    with _wlock, _conn() as c:
        return c.execute(sql, args).lastrowid


def setting(k):
    r = q1("SELECT v FROM settings WHERE k=?", (k,))
    return r["v"] if r else DEFAULTS.get(k, "")


def set_setting(k, v):
    x("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def log(action, detail=""):
    now = int(time.time())
    x("INSERT INTO activity(ts,action,detail) VALUES(?,?,?)", (now, action, str(detail)[:500]))
    x("DELETE FROM activity WHERE ts < ?", (now - 90 * 86400,))

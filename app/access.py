"""Per-request identity + site-level authorization.

The async auth dependency stores the logged-in user in a ContextVar; sync endpoints run in a
thread-pool copy of that context, so every helper below can see who is calling.
No user in context (CLI, background jobs) means internal/admin access.
"""
import json
from contextvars import ContextVar

from fastapi import HTTPException

CURRENT = ContextVar("ypanel_user", default=None)


def set_user(admin_row):
    u = {"id": admin_row["id"], "username": admin_row["username"], "role": admin_row.get("role") or "admin",
         "sites": set(json.loads(admin_row.get("sites") or "[]"))}
    CURRENT.set(u)
    return u


def current():
    return CURRENT.get()


def is_admin():
    u = CURRENT.get()
    return u is None or u["role"] == "admin"


def require_admin():
    if not is_admin():
        raise HTTPException(403, "Admin only")


def can_site(site_id):
    u = CURRENT.get()
    return u is None or u["role"] == "admin" or int(site_id or 0) in u["sites"]


def assert_site(site_id):
    if not can_site(site_id):
        raise HTTPException(404, "Site not found")


def filter_sites(rows, key="site_id"):
    """Filter a list of dict rows down to sites the current user can access."""
    if is_admin():
        return rows
    return [r for r in rows if can_site(r.get(key))]

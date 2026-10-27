#!/usr/bin/env python3
"""YPanel CLI — passwd, 2fa-off, backup-all, set KEY VALUE, info, migrate, update."""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import auth, config, db  # noqa: E402
from app.system import gen_password  # noqa: E402


def cmd_init(a):
    db.init()
    if db.q1("SELECT id FROM admins"):
        print("Admin already exists — use `ypanel passwd` to reset.")
        return
    pw = a.password or gen_password(16)
    db.x("INSERT INTO admins(username,pw_hash,created) VALUES(?,?,?)",
         (a.user, auth.hash_pw(pw), int(time.time())))
    print(f"ADMIN_USER={a.user}\nADMIN_PASS={pw}")


def cmd_passwd(a):
    db.init()
    pw = a.password or gen_password(16)
    row = db.q1("SELECT id FROM admins WHERE username=?", (a.user,))
    if row:
        db.x("UPDATE admins SET pw_hash=? WHERE id=?", (auth.hash_pw(pw), row["id"]))
    else:
        db.x("INSERT INTO admins(username,pw_hash,created) VALUES(?,?,?)",
             (a.user, auth.hash_pw(pw), int(time.time())))
    db.x("DELETE FROM sessions")
    print(f"User: {a.user}\nNew password: {pw}")


def cmd_backup_all(_a):
    db.init()
    from app.api.backups import backup_all
    print(time.strftime("[%Y-%m-%d %H:%M:%S] backup-all start"))
    backup_all()
    print(time.strftime("[%Y-%m-%d %H:%M:%S] backup-all done"))


def cmd_set(a):
    db.init()
    if a.key not in db.DEFAULTS:
        sys.exit(f"Unknown key. Valid: {', '.join(db.DEFAULTS)}")
    db.set_setting(a.key, a.value)
    if a.key == "cf_token":
        from app.api.settings import write_cf_ini
        write_cf_ini(a.value)
    if a.key == "backup_hour":
        from app.api.settings import write_cron_d
        write_cron_d()
    print("ok")


def cmd_2fa_off(a):
    db.init()
    n = db.q1("SELECT id FROM admins WHERE username=?", (a.user,))
    if not n:
        sys.exit("No such admin")
    db.x("UPDATE admins SET totp_secret='', totp_pending='', totp_enabled=0, totp_last=0, recovery='[]' WHERE id=?",
         (n["id"],))
    db.log("2fa.disable", f"{a.user} (cli)")
    print(f"2FA disabled for {a.user}")


def cmd_migrate(_a):
    """v1 → v2: DB columns (db.init) + root-owned site homes for SFTP chroot."""
    import pwd
    db.init()
    for s in db.q("SELECT domain,user FROM sites"):
        home = config.HOME_ROOT / s["domain"]
        try:
            gid = pwd.getpwnam(s["user"]).pw_gid
        except KeyError:
            continue
        if home.is_dir():
            os.chown(home, 0, gid)
            os.chmod(home, 0o750)
            (home / "tmp").mkdir(exist_ok=True)
    from app.api.cron import sync_crontab
    for s in db.q("SELECT DISTINCT s.* FROM sites s JOIN crons c ON c.site_id=s.id"):
        sync_crontab(s)
    print("migration ok")


def cmd_update(_a):
    """Pull the latest version from GitHub and re-run the installer (keeps data)."""
    src = "/usr/local/src/ypanel/get.sh"
    if not os.path.exists(src):
        sys.exit("Installed without git — reinstall once with the curl | bash command from the README.")
    os.execvp("bash", ["bash", src])


def cmd_info(_a):
    db.init()
    print(f"YPanel {config.VERSION}\nData: {config.DATA}")
    for s in db.q("SELECT domain,user,php,ssl FROM sites ORDER BY domain"):
        print(f"  {s['domain']:<40} user={s['user']:<14} php={s['php']} ssl={'yes' if s['ssl'] else 'no'}")


def main():
    p = argparse.ArgumentParser(prog="ypanel")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("--user", default="admin")
    i.add_argument("--password", default="")
    i.set_defaults(fn=cmd_init)
    pw = sub.add_parser("passwd", help="reset admin password")
    pw.add_argument("--user", default="admin")
    pw.add_argument("--password", default="")
    pw.set_defaults(fn=cmd_passwd)
    sub.add_parser("backup-all").set_defaults(fn=cmd_backup_all)
    st = sub.add_parser("set")
    st.add_argument("key")
    st.add_argument("value")
    st.set_defaults(fn=cmd_set)
    sub.add_parser("info").set_defaults(fn=cmd_info)
    tf = sub.add_parser("2fa-off", help="disable 2FA for an admin (lost phone)")
    tf.add_argument("--user", default="admin")
    tf.set_defaults(fn=cmd_2fa_off)
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    sub.add_parser("update", help="update YPanel from GitHub").set_defaults(fn=cmd_update)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()

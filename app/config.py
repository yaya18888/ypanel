"""YPanel configuration — paths are overridable via env for testing."""
import os
from pathlib import Path

VERSION = "3.0.0"

BASE = Path(os.environ.get("YPANEL_HOME", "/opt/ypanel"))
DATA = BASE / "data"
DB_PATH = DATA / "panel.db"
STATIC = BASE / "static"
MYCNF = DATA / "my.cnf"            # [client] creds of ypanel_admin (MariaDB)
CF_INI = DATA / "cloudflare.ini"   # certbot dns-cloudflare credentials
RCLONE_CONF = DATA / "rclone.conf"  # remote backups (S3 / R2 / B2 / Wasabi)
SCRIPTS = BASE / "scripts"
# mail (v3)
POSTFIX_MAPS = Path("/etc/postfix/ypanel")
DOVECOT_USERS = Path("/etc/dovecot/ypanel-users")
VMAIL = Path("/var/vmail")
DKIM_DIR = Path("/var/lib/rspamd/dkim")
DKIM_SELECTOR = "ypanel"
MAIL_STATUS = DATA / "mail-setup.status"
MAIL_LOG = Path("/var/log/ypanel-mail-setup.log")
ROUNDCUBE = BASE / "roundcube"
SFTP_GROUP = "ypsftp"
SFTP_KEYS = Path("/etc/ssh/ypanel_keys")

HOME_ROOT = Path(os.environ.get("YPANEL_HOME_ROOT", "/home"))
BACKUP_ROOT = Path(os.environ.get("YPANEL_BACKUP_ROOT", "/var/backups/ypanel"))
ACME_ROOT = Path("/var/www/acme")
NGINX_SITES = Path("/etc/nginx/sites-available")
NGINX_ENABLED = Path("/etc/nginx/sites-enabled")
NGINX_CUSTOM = Path("/etc/nginx/ypanel/custom")
LE_LIVE = Path("/etc/letsencrypt/live")
CRON_D = Path("/etc/cron.d/ypanel")

COOKIE = "yp_session"
SESSION_TTL = 12 * 3600
SECURE_COOKIE = os.environ.get("YPANEL_INSECURE_COOKIE") != "1"

#!/usr/bin/env bash
# =====================================================================
#  YPanel installer — Ubuntu 22.04 / 24.04, Debian 12  (fresh VPS)
#  Stack: nginx + PHP-FPM (multi-version) + MariaDB + certbot + FastAPI
#  Usage:  bash install.sh [--php="8.1 8.2 8.3"] [--default-php=8.3]
#                          [--port=8090] [--no-firewall] [--force]
#                          [--with-mail=mail.example.com]   (or install mail later from the panel)
#  Re-running it upgrades the panel and keeps all data.
# =====================================================================
set -euo pipefail

YP=/opt/ypanel
PANEL_PORT=8090
APP_PORT=8091
PHP_VERSIONS="8.1 8.2 8.3"
DEFAULT_PHP="8.3"
FIREWALL=1
MAIL_HOST=""
FORCE=0
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for a in "$@"; do
  case "$a" in
    --php=*) PHP_VERSIONS="${a#*=}" ;;
    --default-php=*) DEFAULT_PHP="${a#*=}" ;;
    --port=*) PANEL_PORT="${a#*=}" ;;
    --no-firewall) FIREWALL=0 ;;
    --with-mail=*) MAIL_HOST="${a#*=}" ;;
    --force) FORCE=1 ;;
    *) echo "Unknown option: $a"; exit 1 ;;
  esac
done

step() { echo -e "\n\e[1;34m==> $*\e[0m"; }
ok()   { echo -e "\e[32m✔ $*\e[0m"; }
die()  { echo -e "\e[31m✘ $*\e[0m" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root"
[ -f "$SRC/app/main.py" ] || die "Run this script from the extracted ypanel folder"
case " $PHP_VERSIONS " in *" $DEFAULT_PHP "*) ;; *) die "--default-php must be one of: $PHP_VERSIONS" ;; esac

. /etc/os-release
case "$ID:$VERSION_ID" in
  ubuntu:22.04|ubuntu:24.04|debian:12) ;;
  *) [ $FORCE = 1 ] || die "Unsupported OS: $PRETTY_NAME (use --force to try anyway)" ;;
esac

if [ -d /usr/local/CyberCP ] || [ -d /usr/local/lsws ]; then
  [ $FORCE = 1 ] || die "CyberPanel/LiteSpeed detected. Install YPanel on a FRESH VPS, then migrate your sites."
fi
if ss -ltnp 2>/dev/null | grep -E ':(80|443)\s' | grep -vq nginx; then
  [ $FORCE = 1 ] || die "Ports 80/443 are used by another program (not nginx)."
fi

export DEBIAN_FRONTEND=noninteractive
IPV6=0; [ -f /proc/net/if_inet6 ] && IPV6=1

# ---------------------------------------------------------------------
step "Base packages"
apt-get update -y
apt-get install -y --no-install-recommends ca-certificates curl gnupg lsb-release apt-transport-https \
  nginx mariadb-server mariadb-client certbot python3-certbot-dns-cloudflare \
  python3 python3-venv python3-pip git unzip zip rsync cron fail2ban ufw openssl acl \
  iproute2 tar gzip bzip2 xz-utils util-linux procps

# ---------------------------------------------------------------------
step "PHP repositories"
if [ "$ID" = "ubuntu" ]; then
  apt-get install -y --no-install-recommends software-properties-common
  LC_ALL=C.UTF-8 add-apt-repository -y ppa:ondrej/php
else
  curl -fsSL https://packages.sury.org/php/apt.gpg -o /usr/share/keyrings/deb.sury.org-php.gpg
  echo "deb [signed-by=/usr/share/keyrings/deb.sury.org-php.gpg] https://packages.sury.org/php/ $(lsb_release -sc) main" \
    > /etc/apt/sources.list.d/php.list
fi
apt-get update -y

step "PHP $PHP_VERSIONS"
PKGS=""
for v in $PHP_VERSIONS; do
  for e in fpm cli mysql sqlite3 curl gd mbstring xml zip intl bcmath imagick opcache readline; do
    PKGS="$PKGS php$v-$e"
  done
done
# shellcheck disable=SC2086
apt-get install -y --no-install-recommends $PKGS
for v in $PHP_VERSIONS; do
  W=/etc/php/$v/fpm/pool.d/www.conf
  sed -i 's/^pm = .*/pm = ondemand/; s/^pm.max_children = .*/pm.max_children = 4/' "$W"
  systemctl enable "php$v-fpm" >/dev/null
  systemctl restart "php$v-fpm"
done
update-alternatives --set php "/usr/bin/php$DEFAULT_PHP" >/dev/null 2>&1 || true
ok "PHP ready"

step "rclone (remote backups)"
if ! command -v rclone >/dev/null 2>&1 || ! rclone version 2>/dev/null | head -1 | grep -qE 'v1\.(59|6[0-9]|[7-9][0-9])'; then
  curl -fsSL https://rclone.org/install.sh | bash >/dev/null 2>&1 || true
fi
if command -v rclone >/dev/null 2>&1; then ok "$(rclone version | head -1)"; else echo "⚠ rclone install failed — remote backups unavailable"; fi

step "wp-cli"
curl -fsSL -o /usr/local/bin/wp https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar
chmod +x /usr/local/bin/wp
ok "$(wp --allow-root --version)"

# ---------------------------------------------------------------------
step "MariaDB"
systemctl enable --now mariadb
mkdir -p "$YP/data"
chmod 700 "$YP/data"
if [ ! -f "$YP/data/my.cnf" ]; then
  MPW="$(openssl rand -hex 24)"
  mysql -e "CREATE USER IF NOT EXISTS 'ypanel_admin'@'localhost' IDENTIFIED BY '$MPW';
            ALTER USER 'ypanel_admin'@'localhost' IDENTIFIED BY '$MPW';
            GRANT ALL PRIVILEGES ON *.* TO 'ypanel_admin'@'localhost' WITH GRANT OPTION;
            DROP USER IF EXISTS ''@'localhost';
            DROP DATABASE IF EXISTS test;
            FLUSH PRIVILEGES;"
  umask 077
  printf "[client]\nuser=ypanel_admin\npassword=%s\nsocket=/run/mysqld/mysqld.sock\n" "$MPW" > "$YP/data/my.cnf"
  umask 022
  chmod 600 "$YP/data/my.cnf"
fi
ok "MariaDB admin user ready"

# ---------------------------------------------------------------------
step "Redis (object cache + spam filter storage)"
apt-get install -y --no-install-recommends redis-server redis-tools
if [ ! -f "$YP/data/redis.pass" ]; then
  umask 077; openssl rand -hex 24 > "$YP/data/redis.pass"; umask 022
fi
RPASS="$(cat "$YP/data/redis.pass")"
RVER="$(redis-server --version | grep -o 'v=[0-9]*\.[0-9]*' | cut -d= -f2)"
RMAJ="${RVER%%.*}"; RMIN="${RVER#*.}"
CHAN=""; { [ "$RMAJ" -gt 6 ] || { [ "$RMAJ" -eq 6 ] && [ "$RMIN" -ge 2 ]; }; } && CHAN=" &*"
RCONF=/etc/redis/redis.conf
sed -i 's/^bind .*/bind 127.0.0.1/; s/^# *maxmemory-policy .*/maxmemory-policy allkeys-lru/' "$RCONF"
grep -q '^maxmemory ' "$RCONF" || echo "maxmemory 128mb" >> "$RCONF"
grep -q '^maxmemory-policy ' "$RCONF" || echo "maxmemory-policy allkeys-lru" >> "$RCONF"
sed -i '/^requirepass /d' "$RCONF"
grep -q '^aclfile ' "$RCONF" || echo "aclfile /etc/redis/users.acl" >> "$RCONF"
touch /etc/redis/users.acl
# keep per-site users already saved, (re)write the default admin user
grep -v '^user default ' /etc/redis/users.acl > /etc/redis/users.acl.tmp || true
{ echo "user default on >$RPASS ~*$CHAN +@all"; cat /etc/redis/users.acl.tmp; } > /etc/redis/users.acl
rm -f /etc/redis/users.acl.tmp
chown redis:redis /etc/redis/users.acl; chmod 640 /etc/redis/users.acl
systemctl enable redis-server >/dev/null 2>&1 || true
systemctl restart redis-server
ok "Redis $RVER (password in $YP/data/redis.pass)"

# ---------------------------------------------------------------------
step "Panel application"
mkdir -p "$YP/www"
rsync -a --delete "$SRC/app/" "$YP/app/"
rsync -a --delete "$SRC/static/" "$YP/static/"
install -m 755 "$SRC/cli.py" "$YP/cli.py"
mkdir -p "$YP/scripts"
rsync -a --delete "$SRC/scripts/" "$YP/scripts/"
chmod 755 "$YP"/scripts/*.sh
install -m 644 "$SRC/requirements.txt" "$YP/requirements.txt"
[ -d "$YP/venv" ] || python3 -m venv "$YP/venv"
"$YP/venv/bin/pip" install -q --upgrade pip
"$YP/venv/bin/pip" install -q -r "$YP/requirements.txt"
cat > /usr/local/bin/ypanel <<EOF
#!/bin/sh
exec $YP/venv/bin/python $YP/cli.py "\$@"
EOF
chmod 755 /usr/local/bin/ypanel
/usr/local/bin/ypanel migrate >/dev/null
/usr/local/bin/ypanel set panel_port "$PANEL_PORT" >/dev/null
ok "Installed to $YP"

# ---------------------------------------------------------------------
step "nginx base config"
mkdir -p /var/www/acme/.well-known/acme-challenge /var/backups/ypanel /etc/nginx/ypanel/custom "$YP/data/ssl"
chmod 700 /var/backups/ypanel
rm -f /etc/nginx/sites-enabled/default

if [ ! -f "$YP/data/ssl/panel.crt" ]; then
  openssl req -x509 -nodes -newkey rsa:2048 -days 3650 -subj "/CN=ypanel" \
    -keyout "$YP/data/ssl/panel.key" -out "$YP/data/ssl/panel.crt" 2>/dev/null
  chmod 600 "$YP/data/ssl/panel.key"
fi

cat > /etc/nginx/snippets/ypanel-acme.conf <<'EOF'
location ^~ /.well-known/acme-challenge/ {
    root /var/www/acme;
    default_type "text/plain";
    try_files $uri =404;
}
EOF

cat > /etc/nginx/snippets/ypanel-common.conf <<'EOF'
location = /favicon.ico { log_not_found off; access_log off; try_files $uri =404; }
location = /robots.txt  { log_not_found off; access_log off; try_files $uri /index.php?$args; }
location ~ /\.(?!well-known) { deny all; }
location ~* \.(sqlite|sqlite3|db|sql|bak|old|swp|env|log|sh|ini)$ { deny all; }
location ~* \.(?:css|js|mjs|jpe?g|gif|png|webp|avif|svg|ico|woff2?|ttf|eot|mp4|webm)$ {
    expires 30d;
    add_header Cache-Control "public";
    access_log off;
    try_files $uri /index.php?$args;
}
EOF

cat > /etc/nginx/snippets/ypanel-ssl.conf <<'EOF'
ssl_protocols TLSv1.2 TLSv1.3;
ssl_prefer_server_ciphers off;
ssl_session_cache shared:YPSSL:10m;
ssl_session_timeout 1d;
ssl_session_tickets off;
EOF

cat > /etc/nginx/conf.d/ypanel.conf <<'EOF'
server_tokens off;
server_names_hash_bucket_size 128;
gzip_vary on;
gzip_proxied any;
gzip_comp_level 5;
gzip_min_length 1024;
gzip_types text/plain text/css text/xml application/json application/javascript application/xml application/rss+xml image/svg+xml;
EOF

# Rotate per-site logs (stats read the rotated .gz files too)
cat > /etc/logrotate.d/ypanel-sites <<'EOF'
/home/*/logs/*.log {
    daily
    rotate 30
    missingok
    notifempty
    compress
    delaycompress
    create
    su root root
    sharedscripts
    postrotate
        [ -f /run/nginx.pid ] && kill -USR1 "$(cat /run/nginx.pid)" || true
    endscript
}
EOF

# Cloudflare real visitor IP
CF4="$(curl -fsSL --max-time 15 https://www.cloudflare.com/ips-v4 || true)"
CF6="$(curl -fsSL --max-time 15 https://www.cloudflare.com/ips-v6 || true)"
if [ -n "$CF4" ]; then
  {
    echo "# Cloudflare IP ranges — generated by YPanel"
    for ip in $CF4 $CF6; do echo "set_real_ip_from $ip;"; done
    echo "real_ip_header CF-Connecting-IP;"
  } > /etc/nginx/conf.d/cloudflare-realip.conf
fi

L6_80=""; L6_443=""; L6_P=""
if [ $IPV6 = 1 ]; then
  L6_80="listen [::]:80 default_server;"
  L6_443="listen [::]:443 ssl default_server;"
  L6_P="listen [::]:$PANEL_PORT ssl http2;"
fi

cat > /etc/nginx/sites-available/000-default.conf <<EOF
# Catch-all: unknown hosts get dropped, ACME challenges still work
server {
    listen 80 default_server;
    $L6_80
    server_name _;
    include snippets/ypanel-acme.conf;
    location / { return 444; }
}
server {
    listen 443 ssl default_server;
    $L6_443
    server_name _;
    ssl_certificate $YP/data/ssl/panel.crt;
    ssl_certificate_key $YP/data/ssl/panel.key;
    return 444;
}
EOF
ln -sf /etc/nginx/sites-available/000-default.conf /etc/nginx/sites-enabled/000-default.conf

# ---------------------------------------------------------------------
step "phpMyAdmin"
if [ ! -d "$YP/www/phpmyadmin" ]; then
  T="$(mktemp -d)"
  curl -fsSL -o "$T/pma.tgz" https://www.phpmyadmin.net/downloads/phpMyAdmin-latest-all-languages.tar.gz
  tar -xzf "$T/pma.tgz" -C "$T"
  mv "$T"/phpMyAdmin-*-all-languages "$YP/www/phpmyadmin"
  rm -rf "$T"
  SECRET="$(openssl rand -hex 16)"
  cat > "$YP/www/phpmyadmin/config.inc.php" <<EOF
<?php
\$cfg['blowfish_secret'] = '$SECRET';
\$i = 1;
\$cfg['Servers'][\$i]['auth_type'] = 'cookie';
\$cfg['Servers'][\$i]['host'] = 'localhost';
\$cfg['Servers'][\$i]['AllowNoPassword'] = false;
\$cfg['TempDir'] = '$YP/www/phpmyadmin/tmp';
\$cfg['UploadDir'] = '';
\$cfg['SaveDir'] = '';
\$cfg['VersionCheck'] = false;
EOF
  mkdir -p "$YP/www/phpmyadmin/tmp"
  chown -R www-data:www-data "$YP/www/phpmyadmin/tmp"
  chown root:www-data "$YP/www/phpmyadmin/config.inc.php"
  chmod 640 "$YP/www/phpmyadmin/config.inc.php"
fi
chmod 755 "$YP" "$YP/www"
ok "phpMyAdmin ready (protected by panel login)"

# ---------------------------------------------------------------------
step "Panel vhost on port $PANEL_PORT"
cat > /etc/nginx/sites-available/ypanel.conf <<EOF
server {
    listen $PANEL_PORT ssl http2;
    $L6_P
    server_name _;
    ssl_certificate $YP/data/ssl/panel.crt;
    ssl_certificate_key $YP/data/ssl/panel.key;
    include snippets/ypanel-ssl.conf;
    error_page 497 =301 https://\$host:$PANEL_PORT\$request_uri;
    client_max_body_size 2048m;
    root $YP/www;
    access_log /var/log/nginx/ypanel.access.log;

    location = /_auth {
        internal;
        proxy_pass http://127.0.0.1:$APP_PORT/api/session-check;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header Cookie \$http_cookie;
    }
    location @login { return 302 /; }

    location ^~ /webmail/ {
        index index.php;
        location ~ /\. { deny all; }
        location ~ \.php\$ {
            try_files \$uri =404;
            include fastcgi_params;
            fastcgi_param SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
            fastcgi_pass unix:/run/php/php$DEFAULT_PHP-fpm.sock;
        }
    }

    location ^~ /phpmyadmin/ {
        auth_request /_auth;
        error_page 401 = @login;
        index index.php;
        location ~ \.php\$ {
            auth_request /_auth;
            error_page 401 = @login;
            try_files \$uri =404;
            include fastcgi_params;
            fastcgi_param SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
            fastcgi_pass unix:/run/php/php$DEFAULT_PHP-fpm.sock;
        }
    }

    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 900s;
        proxy_send_timeout 900s;
        proxy_request_buffering off;
    }
}
EOF
ln -sf /etc/nginx/sites-available/ypanel.conf /etc/nginx/sites-enabled/ypanel.conf
nginx -t
systemctl enable nginx >/dev/null
systemctl reload nginx || systemctl restart nginx

# ---------------------------------------------------------------------
step "Panel service"
cat > /etc/systemd/system/ypanel.service <<EOF
[Unit]
Description=YPanel control panel
After=network.target mariadb.service

[Service]
Type=simple
WorkingDirectory=$YP
Environment=PYTHONUNBUFFERED=1
ExecStart=$YP/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $APP_PORT --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-server-header
Restart=always
RestartSec=3
KillMode=process

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable ypanel >/dev/null
systemctl restart ypanel

CREDS=""
if ! "$YP/venv/bin/python" - <<EOF
import sqlite3,sys
try:
    c=sqlite3.connect("$YP/data/panel.db"); sys.exit(0 if c.execute("select count(*) from admins").fetchone()[0] else 1)
except Exception: sys.exit(1)
EOF
then
  CREDS="$(/usr/local/bin/ypanel init --user admin)"
  echo "$CREDS" > /root/ypanel-credentials.txt
  chmod 600 /root/ypanel-credentials.txt
  # first install only — later changes are made from Settings
  /usr/local/bin/ypanel set default_php "$DEFAULT_PHP" >/dev/null
  /usr/local/bin/ypanel set backup_hour 3 >/dev/null   # writes /etc/cron.d/ypanel (daily 03:17)
fi

# ---------------------------------------------------------------------
step "Security: fail2ban + firewall"
SSH_PORT="$(sshd -T 2>/dev/null | awk '/^port /{print $2; exit}')"
SSH_PORT="${SSH_PORT:-22}"
cat > /etc/fail2ban/jail.d/ypanel.local <<EOF
[sshd]
enabled = true
port = $SSH_PORT
maxretry = 5
bantime = 1h
EOF
systemctl enable --now fail2ban >/dev/null 2>&1 || true
systemctl restart fail2ban || true
if [ $FIREWALL = 1 ]; then
  ufw allow "$SSH_PORT/tcp" >/dev/null
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw allow "$PANEL_PORT/tcp" >/dev/null
  ufw --force enable >/dev/null
  ok "ufw: $SSH_PORT, 80, 443, $PANEL_PORT open"
fi

# chrooted SFTP for site users
groupadd -f ypsftp
mkdir -p /etc/ssh/ypanel_keys && chmod 755 /etc/ssh/ypanel_keys
SSHD=/etc/ssh/sshd_config
if ! grep -q "BEGIN YPANEL SFTP" "$SSHD"; then
  cp "$SSHD" "$SSHD.ypanel.bak"
  cat >> "$SSHD" <<'SSHEOF'

# BEGIN YPANEL SFTP — chrooted SFTP for site users (managed by YPanel)
Match Group ypsftp
    ChrootDirectory %h
    ForceCommand internal-sftp -u 0022
    PasswordAuthentication yes
    PubkeyAuthentication yes
    AuthorizedKeysFile /etc/ssh/ypanel_keys/%u
    AllowTcpForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTunnel no
# END YPANEL SFTP
SSHEOF
  if sshd -t 2>/dev/null; then
    systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
    ok "SFTP chroot configured"
  else
    cp "$SSHD.ypanel.bak" "$SSHD"
    echo "⚠ sshd rejected the SFTP block — restored original config (SFTP disabled)"
    groupdel ypsftp 2>/dev/null || true
  fi
fi

# swap for small VPS
if [ "$(swapon --show --noheadings | wc -l)" -eq 0 ]; then
  RAM_MB="$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)"
  if [ "$RAM_MB" -lt 4096 ]; then
    fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    ok "2G swap created"
  fi
fi

systemctl enable --now cron >/dev/null 2>&1 || true
sleep 2
systemctl is-active --quiet ypanel || die "ypanel service failed — check: journalctl -u ypanel -n 50"

if [ -n "$MAIL_HOST" ]; then
  step "Mail server ($MAIL_HOST)"
  /usr/local/bin/ypanel set mail_hostname "$MAIL_HOST" >/dev/null
  bash "$YP/scripts/mail-setup.sh" "$MAIL_HOST" "$YP" 2>&1 | tee -a /var/log/ypanel-mail-setup.log
fi

IP="$(curl -fs4 --max-time 8 https://api.ipify.org || hostname -I | awk '{print $1}')"
echo
echo -e "\e[1;32m================================================================\e[0m"
echo -e "\e[1;32m  YPanel is ready → https://$IP:$PANEL_PORT\e[0m"
if [ -n "$CREDS" ]; then
  echo "$CREDS" | sed 's/^/  /'
  echo "  (saved in /root/ypanel-credentials.txt — delete it after login)"
else
  echo "  Existing admin kept. Reset with: ypanel passwd"
fi
echo "  Browser will warn about the self-signed cert — that's expected."
echo -e "\e[1;32m================================================================\e[0m"

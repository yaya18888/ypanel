#!/usr/bin/env bash
# =====================================================================
#  YPanel mail stack: Postfix + Dovecot (IMAP/POP3/LMTP/Sieve) + Rspamd (spam + DKIM) + Roundcube
#  Usage: mail-setup.sh <mail.hostname> [/opt/ypanel]      (run by the panel, or by hand as root)
# =====================================================================
set -euo pipefail
HOST="${1:?mail hostname required}"
YP="${2:-/opt/ypanel}"
STATUS="$YP/data/mail-setup.status"
echo running > "$STATUS"
trap 'echo failed > "$STATUS"; echo "✘ mail setup failed (line $LINENO)"' ERR
export DEBIAN_FRONTEND=noninteractive
step() { echo -e "\n==> $*"; }

[ -f "$YP/data/redis.pass" ] || { echo "Redis not configured — re-run install.sh first"; exit 1; }
REDIS_PASS="$(cat "$YP/data/redis.pass")"

step "Packages"
echo "postfix postfix/main_mailer_type select Internet Site" | debconf-set-selections
echo "postfix postfix/mailname string $HOST" | debconf-set-selections
apt-get update -y
apt-get install -y --no-install-recommends postfix dovecot-core dovecot-imapd dovecot-pop3d dovecot-lmtpd \
  dovecot-sieve rspamd dnsutils ssl-cert openssl

step "vmail user"
getent group vmail >/dev/null || groupadd -r vmail
id vmail >/dev/null 2>&1 || useradd -r -g vmail -d /var/vmail -s /usr/sbin/nologin vmail
VUID="$(id -u vmail)"; VGID="$(id -g vmail)"
# hosts with IPv6 disabled must not bind ::
if [ -f /proc/net/if_inet6 ]; then INET_PROTO=all; DOVE_LISTEN="*, ::"; MYNET="127.0.0.0/8 [::1]/128"
else INET_PROTO=ipv4; DOVE_LISTEN="*"; MYNET="127.0.0.0/8"; fi
mkdir -p /var/vmail
chown vmail:vmail /var/vmail
chmod 770 /var/vmail
echo "$HOST" > /etc/mailname

step "Postfix"
mkdir -p /etc/postfix/ypanel
for f in domains mailboxes aliases senders; do
  [ -f "/etc/postfix/ypanel/$f" ] || echo "# Managed by YPanel" > "/etc/postfix/ypanel/$f"
  postmap "hash:/etc/postfix/ypanel/$f"
done
SNAKE_CERT=/etc/ssl/certs/ssl-cert-snakeoil.pem
SNAKE_KEY=/etc/ssl/private/ssl-cert-snakeoil.key
[ -f "$SNAKE_CERT" ] || make-ssl-cert generate-default-snakeoil --force-overwrite
CERT="$SNAKE_CERT"; KEY="$SNAKE_KEY"
if [ -f "/etc/letsencrypt/live/$HOST/fullchain.pem" ]; then
  CERT="/etc/letsencrypt/live/$HOST/fullchain.pem"; KEY="/etc/letsencrypt/live/$HOST/privkey.pem"
fi
postconf -e \
  "myhostname = $HOST" \
  "myorigin = /etc/mailname" \
  "mydestination = localhost" \
  "inet_interfaces = all" \
  "inet_protocols = $INET_PROTO" \
  "mynetworks = $MYNET" \
  "smtpd_relay_restrictions = permit_mynetworks, permit_sasl_authenticated, reject_unauth_destination" \
  "virtual_mailbox_domains = hash:/etc/postfix/ypanel/domains" \
  "virtual_mailbox_maps = hash:/etc/postfix/ypanel/mailboxes" \
  "virtual_alias_maps = hash:/etc/postfix/ypanel/aliases" \
  "virtual_transport = lmtp:unix:private/dovecot-lmtp" \
  "smtpd_sender_login_maps = hash:/etc/postfix/ypanel/senders" \
  "smtpd_tls_cert_file = $CERT" \
  "smtpd_tls_key_file = $KEY" \
  "smtpd_tls_security_level = may" \
  "smtpd_tls_protocols = !SSLv2, !SSLv3, !TLSv1, !TLSv1.1" \
  "smtpd_tls_mandatory_protocols = !SSLv2, !SSLv3, !TLSv1, !TLSv1.1" \
  "smtp_tls_security_level = may" \
  "smtpd_tls_auth_only = yes" \
  "smtpd_sasl_type = dovecot" \
  "smtpd_sasl_path = private/auth" \
  "smtpd_sasl_auth_enable = no" \
  "smtpd_helo_required = yes" \
  "disable_vrfy_command = yes" \
  "strict_rfc821_envelopes = yes" \
  "smtpd_recipient_restrictions = permit_mynetworks, permit_sasl_authenticated, reject_unauth_destination, reject_unknown_recipient_domain" \
  "message_size_limit = 52428800" \
  "mailbox_size_limit = 0" \
  "recipient_delimiter = +" \
  "milter_protocol = 6" \
  "milter_default_action = accept" \
  "milter_mail_macros = i {mail_addr} {client_addr} {client_name} {auth_authen}" \
  "smtpd_milters = inet:127.0.0.1:11332" \
  "non_smtpd_milters = inet:127.0.0.1:11332"

for svc in submission smtps; do
  postconf -M "$svc/inet=$svc inet n - y - - smtpd"
  postconf -P "$svc/inet/syslog_name=postfix/$svc"
  postconf -P "$svc/inet/smtpd_sasl_auth_enable=yes"
  postconf -P "$svc/inet/smtpd_client_restrictions=permit_sasl_authenticated,reject"
  postconf -P "$svc/inet/smtpd_sender_restrictions=reject_sender_login_mismatch"
  postconf -P "$svc/inet/smtpd_recipient_restrictions=permit_sasl_authenticated,reject"
  postconf -P "$svc/inet/milter_macro_daemon_name=ORIGINATING"
done
postconf -P "submission/inet/smtpd_tls_security_level=encrypt"
postconf -P "smtps/inet/smtpd_tls_wrappermode=yes"
postfix check

step "Dovecot"
sed -i 's/^!include auth-system.conf.ext/#!include auth-system.conf.ext/' /etc/dovecot/conf.d/10-auth.conf
touch /etc/dovecot/ypanel-users
chown root:dovecot /etc/dovecot/ypanel-users
chmod 640 /etc/dovecot/ypanel-users
cat > /etc/dovecot/conf.d/99-ypanel.conf <<EOF
# Managed by YPanel
protocols = imap pop3 lmtp
listen = $DOVE_LISTEN
mail_location = maildir:/var/vmail/%d/%n
mail_uid = vmail
mail_gid = vmail
first_valid_uid = $VUID
last_valid_uid = $VUID
first_valid_gid = $VGID
last_valid_gid = $VGID
auth_mechanisms = plain login
disable_plaintext_auth = yes
ssl = required
ssl_min_protocol = TLSv1.2
mail_plugins = \$mail_plugins quota

passdb {
  driver = passwd-file
  args = scheme=SHA512-CRYPT username_format=%u /etc/dovecot/ypanel-users
}
userdb {
  driver = passwd-file
  args = username_format=%u /etc/dovecot/ypanel-users
  default_fields = uid=vmail gid=vmail home=/var/vmail/%d/%n
}

# plaintext IMAP/POP3 ports closed — clients use 993 / 995 (TLS)
service imap-login {
  inet_listener imap {
    port = 0
  }
}
service pop3-login {
  inet_listener pop3 {
    port = 0
  }
}

service lmtp {
  unix_listener /var/spool/postfix/private/dovecot-lmtp {
    mode = 0600
    user = postfix
    group = postfix
  }
}
service auth {
  unix_listener /var/spool/postfix/private/auth {
    mode = 0660
    user = postfix
    group = postfix
  }
}

protocol lmtp {
  mail_plugins = \$mail_plugins sieve
  postmaster_address = postmaster@$HOST
}
protocol imap {
  mail_plugins = \$mail_plugins imap_quota
}

plugin {
  quota = maildir:User quota
  quota_grace = 10%%
  quota_exceeded_message = Mailbox is full
  sieve_before = /etc/dovecot/sieve/spam-to-junk.sieve
}

namespace inbox {
  mailbox Drafts {
    auto = subscribe
    special_use = \Drafts
  }
  mailbox Sent {
    auto = subscribe
    special_use = \Sent
  }
  mailbox Trash {
    auto = subscribe
    special_use = \Trash
  }
  mailbox Junk {
    auto = subscribe
    special_use = \Junk
  }
}
EOF
if [ ! -f /etc/dovecot/conf.d/99-ypanel-ssl.conf ]; then
  printf "# Managed by YPanel\nssl_cert = <%s\nssl_key = <%s\n" "$CERT" "$KEY" > /etc/dovecot/conf.d/99-ypanel-ssl.conf
fi
mkdir -p /etc/dovecot/sieve
cat > /etc/dovecot/sieve/spam-to-junk.sieve <<'EOF'
require ["fileinto", "mailbox"];
if anyof (header :contains "X-Spam" "Yes", header :contains "X-Spam-Flag" "YES") {
  fileinto :create "Junk";
  stop;
}
EOF
sievec /etc/dovecot/sieve/spam-to-junk.sieve
doveconf -n >/dev/null

step "Rspamd (spam filter + DKIM signing)"
mkdir -p /var/lib/rspamd/dkim /etc/rspamd/local.d
chown _rspamd:_rspamd /var/lib/rspamd/dkim
chmod 750 /var/lib/rspamd/dkim
cat > /etc/rspamd/local.d/redis.conf <<EOF
servers = "127.0.0.1:6379";
password = "$REDIS_PASS";
EOF
chmod 640 /etc/rspamd/local.d/redis.conf
chown root:_rspamd /etc/rspamd/local.d/redis.conf
for f in dkim_signing arc; do
cat > "/etc/rspamd/local.d/$f.conf" <<'EOF'
# Managed by YPanel — keys: /var/lib/rspamd/dkim/<domain>.ypanel.key
enabled = true;
allow_username_mismatch = true;
sign_authenticated = true;
sign_local = true;
use_domain = "header";
use_esld = true;
selector = "ypanel";
path = "/var/lib/rspamd/dkim/$domain.$selector.key";
EOF
done
cat > /etc/rspamd/local.d/milter_headers.conf <<'EOF'
use = ["x-spamd-bar", "x-spam-level", "authentication-results"];
extended_spam_headers = true;
EOF
cat > /etc/rspamd/local.d/actions.conf <<'EOF'
reject = 15;
add_header = 6;
greylist = 4;
EOF
rspamadm configtest

step "Roundcube webmail"
RC="$YP/roundcube"
if [ ! -d "$RC/public_html" ]; then
  URL="$(curl -fsSL https://api.github.com/repos/roundcube/roundcubemail/releases/latest \
        | grep -o 'https://[^"]*roundcubemail-[0-9.]*-complete\.tar\.gz' | head -1 || true)"
  if [ -n "$URL" ]; then
    T="$(mktemp -d)"
    curl -fsSL -o "$T/rc.tgz" "$URL"
    tar -xzf "$T/rc.tgz" -C "$T"
    mv "$T"/roundcubemail-* "$RC"
    rm -rf "$T"
  else
    echo "⚠ could not find Roundcube release — webmail skipped"
  fi
fi
if [ -d "$RC/public_html" ] && [ ! -f "$RC/config/config.inc.php" ]; then
  mkdir -p "$RC/db"
  DES="$(openssl rand -hex 12)"
  cat > "$RC/config/config.inc.php" <<EOF
<?php
\$config['db_dsnw'] = 'sqlite:///$RC/db/roundcube.db?mode=0640';
\$config['imap_host'] = 'ssl://127.0.0.1:993';
\$config['smtp_host'] = 'tls://127.0.0.1:587';
\$config['smtp_user'] = '%u';
\$config['smtp_pass'] = '%p';
\$config['imap_conn_options'] = ['ssl' => ['verify_peer' => false, 'verify_peer_name' => false]];
\$config['smtp_conn_options'] = ['ssl' => ['verify_peer' => false, 'verify_peer_name' => false]];
\$config['support_url'] = '';
\$config['product_name'] = 'Webmail';
\$config['des_key'] = '$DES';
\$config['plugins'] = ['archive', 'zipdownload', 'markasjunk'];
\$config['skin'] = 'elastic';
\$config['enable_installer'] = false;
\$config['login_autocomplete'] = 2;
\$config['ip_check'] = true;
EOF
  chown -R www-data:www-data "$RC/db" "$RC/temp" "$RC/logs"
  chown root:www-data "$RC/config/config.inc.php"; chmod 640 "$RC/config/config.inc.php"
  rm -rf "$RC/installer"
  runuser -u www-data -- php "$RC/bin/initdb.sh" --dir="$RC/SQL" || echo "⚠ roundcube initdb failed (it will retry on first login)"
fi
if [ -d "$RC/public_html" ]; then
  ln -sfn "$RC/public_html" "$YP/www/webmail"
fi

step "Firewall + services"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  for p in 25 465 587 993 995; do ufw allow "$p/tcp" >/dev/null; done
fi
for s in rspamd dovecot postfix; do systemctl enable "$s" >/dev/null 2>&1 || true; systemctl restart "$s"; done
echo done > "$STATUS"
echo "✔ Mail stack ready for $HOST"

# YPanel v3.0

Lightweight cPanel-style hosting panel — nginx + PHP-FPM (multi-version) + MariaDB + Let's Encrypt, managed by a FastAPI backend and a mobile-friendly web UI.

## Install (fresh VPS — Ubuntu 22.04/24.04 or Debian 12)

```bash
# upload ypanel-v3.0.0.zip to the server, then:
apt-get install -y unzip && unzip ypanel-v3.0.0.zip && cd ypanel
bash install.sh
# options: --php="8.1 8.2 8.3" --default-php=8.3 --port=8090 --no-firewall --with-mail=mail.example.com
```

Open `https://SERVER_IP:8090` — credentials are printed at the end and saved in `/root/ypanel-credentials.txt`.

> Do **not** install on a server running CyberPanel/LiteSpeed (ports 80/443 conflict). Use a fresh VPS and migrate sites.

## Features

| Area | What it does |
|---|---|
| Websites | Linux user per site, isolated PHP-FPM pool (ondemand), nginx vhost, www toggle, PHP version switch, per-site php.ini (memory, upload, timeouts, open_basedir), custom nginx directives (auto-rollback on `nginx -t` failure), logs |
| SSL | Let's Encrypt via HTTP-01 (shared `/var/www/acme`) or Cloudflare DNS-01 (works behind orange cloud), ECDSA, auto-renew + nginx reload |
| Databases | MariaDB DB + user (`<siteuser>_<name>`), password reset, sizes, phpMyAdmin at `/phpmyadmin/` (requires panel login) |
| File manager | Browse, edit (Ctrl+S), upload, mkdir, rename, chmod, delete, zip, extract (runs as the site user), download |
| Cron | Per-site crontab running as the site user, presets |
| Backups | `public_html` + SQL dumps → `/var/backups/ypanel/<domain>/`, restore, download, retention, daily schedule, exclude paths |
| Monitoring | CPU/RAM/disk/load, services (restart/start/stop), top processes, activity log |
| Installers | WordPress (wp-cli), Git deploy, clone files from another site, ZIP upload |
| Cloudflare | A records (proxied / DNS-only), real visitor IP in nginx logs |
| **2FA (v2)** | TOTP (Google Authenticator / Authy / 2FAS) with QR setup, 8 one-time recovery codes, replay protection, `ypanel 2fa-off` |
| **SFTP (v2)** | Per-site chrooted SFTP (password and/or SSH keys), jailed to `/home/<domain>`, no shell, no tunnels |
| **DNS (v2)** | Cloudflare manager: all zones, records CRUD, orange-cloud toggle, SSL mode / Always HTTPS / security level / dev mode, cache purge (all or URLs) |
| **Remote backups (v2)** | rclone → Cloudflare R2 / Backblaze B2 / Wasabi / AWS S3 / MinIO, auto-upload after each backup, remote retention, list + fetch back for restore |
| **Firewall (v2)** | ufw rules (allow/deny/limit, ports, ranges, source IP), block IP/CIDR, protected SSH/web/panel rules, fail2ban jails + unban |
| **Email (v3)** | Postfix + Dovecot + Rspamd + Roundcube: domains, mailboxes (quotas, suspend), forwarders + catch-all, send-as alias, DKIM per domain, one-click SPF/DKIM/DMARC/MX on Cloudflare, live DNS check, queue manager, Let's Encrypt for the mail host, spam → Junk (Sieve), port-25 / PTR checks |
| **Users (v3)** | Admins + site users; a site user only sees assigned websites (files, DBs, cron, backups, SFTP, cache, stats) — server, DNS, firewall, mail, settings stay admin-only |
| **Redis (v3)** | Shared Redis with one ACL user per site (keys jailed to `<user>:*`, no FLUSHALL/KEYS), one-click WordPress Redis Object Cache, flush, max-memory setting |
| **Traffic stats (v3)** | Per-site pageviews, unique visitors, bandwidth, top pages, referrers, bots, 404s, status codes — from nginx logs (rotated daily, 30 days kept) |
| Security | bcrypt + hashed sessions, login lockout, CSRF header, panel on HTTPS, ufw, fail2ban (sshd), 2G swap on small VPS |

## CLI

```bash
ypanel passwd                 # reset admin password
ypanel 2fa-off                # disable 2FA (lost phone)
ypanel migrate                # v1 → v2 (install.sh runs it automatically)
ypanel info                   # list sites
ypanel backup-all             # run backups now
ypanel set admin_email you@example.com
```

## Paths

- Panel: `/opt/ypanel` (data: `/opt/ypanel/data`, service: `systemctl status ypanel`)
- Sites: `/home/<domain>/{public_html,logs,tmp}`
- Vhosts: `/etc/nginx/sites-available/<domain>.conf`, custom rules: `/etc/nginx/ypanel/custom/<domain>.conf`
- Pools: `/etc/php/<ver>/fpm/pool.d/<user>.conf`

## Notes

- Behind Cloudflare proxy, set SSL mode to **Full (strict)** after issuing the certificate (Flexible causes redirect loops).
- Panel uses a self-signed cert on port 8090; put it behind a Cloudflare-proxied hostname if you want a valid one.

## Upgrade v1 → v2

Upload the new zip and re-run `bash install.sh` — data, sites and settings are kept; DB columns are migrated and site homes become root-owned (required for the SFTP chroot).

## Cloudflare token permissions

Zone · Zone · Read, Zone · DNS · Edit, Zone · Zone Settings · Edit, Zone · Cache Purge · Purge.

## Email notes

- Many VPS providers block outbound port 25 — the Email page has a check. If blocked, ask the provider to open it.
- Set the server IP's reverse DNS (PTR) to the mail hostname in your VPS provider panel.
- The mail host A record must be DNS-only (grey cloud).
- Mail clients: IMAP 993 (SSL), POP3 995, SMTP 587 (STARTTLS) / 465 (SSL), username = full address.

## Upgrade v2 → v3

Re-run `bash install.sh` with the new zip (installs Redis, logrotate, scripts; migrates the DB). Mail is installed from the Email page or with `--with-mail=`.

"""nginx vhost + PHP-FPM pool generators."""
from . import config
from .system import has_ipv6, size_mb

INI_DEFAULTS = {
    "memory_limit": "256M",
    "upload_max_filesize": "128M",
    "post_max_size": "128M",
    "max_execution_time": 120,
    "max_input_time": 120,
    "max_input_vars": 5000,
    "max_children": 5,
    "open_basedir": True,
    "display_errors": False,
}


def _listen(port, ssl=False):
    extra = " ssl http2" if ssl else ""
    s = f"    listen {port}{extra};\n"
    if has_ipv6():
        s += f"    listen [::]:{port}{extra};\n"
    return s


def vhost(site):
    d = site["domain"]
    names = f"{d} www.{d}" if site["www"] else d
    home = config.HOME_ROOT / d
    sock = f"/run/php/{site['user']}-{site['php']}.sock"
    ini = {**INI_DEFAULTS, **(site.get("ini") or {})}
    body_mb = max(size_mb(ini["post_max_size"]), size_mb(ini["upload_max_filesize"]))
    read_timeout = max(60, int(ini["max_execution_time"]) + 30)

    inner = f"""
    root {home}/public_html;
    index index.php index.html index.htm;
    access_log {home}/logs/access.log;
    error_log {home}/logs/error.log warn;
    client_max_body_size {body_mb}m;

    include {config.NGINX_CUSTOM}/{d}.conf;
    include snippets/ypanel-acme.conf;
    include snippets/ypanel-common.conf;

    location / {{
        try_files $uri $uri/ /index.php?$args;
    }}

    location ~ \\.php$ {{
        try_files $uri =404;
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
        fastcgi_pass unix:{sock};
        fastcgi_read_timeout {read_timeout}s;
        fastcgi_buffers 16 16k;
        fastcgi_buffer_size 32k;
    }}
"""
    head = f"# Managed by YPanel — edit custom rules in {config.NGINX_CUSTOM}/{d}.conf\n"
    if not site["ssl"]:
        return head + "server {\n" + _listen(80) + f"    server_name {names};\n" + inner + "}\n"

    return (
        head
        + "server {\n" + _listen(80) + f"    server_name {names};\n"
        + "    include snippets/ypanel-acme.conf;\n"
        + "    location / { return 301 https://$host$request_uri; }\n}\n\n"
        + "server {\n" + _listen(443, ssl=True) + f"    server_name {names};\n"
        + f"    ssl_certificate {config.LE_LIVE}/{d}/fullchain.pem;\n"
        + f"    ssl_certificate_key {config.LE_LIVE}/{d}/privkey.pem;\n"
        + "    include snippets/ypanel-ssl.conf;\n"
        + inner + "}\n"
    )


def fpm_pool(site):
    d, u, v = site["domain"], site["user"], site["php"]
    home = config.HOME_ROOT / d
    ini = {**INI_DEFAULTS, **(site.get("ini") or {})}
    lines = [
        f"; Managed by YPanel — {d}",
        f"[{u}]",
        f"user = {u}",
        f"group = {u}",
        f"listen = /run/php/{u}-{v}.sock",
        "listen.owner = www-data",
        "listen.group = www-data",
        "listen.mode = 0660",
        "pm = ondemand",
        f"pm.max_children = {int(ini['max_children'])}",
        "pm.process_idle_timeout = 30s",
        "pm.max_requests = 1000",
        "chdir = /",
        "catch_workers_output = yes",
        f"php_value[memory_limit] = {ini['memory_limit']}",
        f"php_value[max_execution_time] = {int(ini['max_execution_time'])}",
        f"php_value[max_input_time] = {int(ini['max_input_time'])}",
        f"php_value[max_input_vars] = {int(ini['max_input_vars'])}",
        f"php_admin_value[upload_max_filesize] = {ini['upload_max_filesize']}",
        f"php_admin_value[post_max_size] = {ini['post_max_size']}",
        f"php_admin_value[upload_tmp_dir] = {home}/tmp",
        f"php_admin_value[session.save_path] = {home}/tmp",
        f"php_admin_value[sys_temp_dir] = {home}/tmp",
        f"php_admin_value[error_log] = {home}/logs/php-error.log",
        "php_admin_flag[log_errors] = on",
        f"php_flag[display_errors] = {'on' if ini['display_errors'] else 'off'}",
    ]
    if ini["open_basedir"]:
        lines.append(f"php_admin_value[open_basedir] = {home}/:/tmp/:/usr/share/php/:/dev/urandom")
    return "\n".join(lines) + "\n"

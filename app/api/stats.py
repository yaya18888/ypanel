"""Per-site traffic stats parsed from nginx access logs (combined format, incl. rotated .gz)."""
import gzip
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter

from .. import config
from ..system import bad
from .sites import get_site

router = APIRouter(prefix="/api/stats", tags=["stats"])

LINE_RE = re.compile(r'^(\S+) \S+ \S+ \[([^\]]+)\] "(\S+) (\S+)[^"]*" (\d{3}) (\d+|-) "([^"]*)" "([^"]*)"')
STATIC_RE = re.compile(r"\.(?:css|js|mjs|map|jpe?g|png|gif|webp|avif|svg|ico|woff2?|ttf|eot|mp4|webm|json|xml|txt)$", re.I)
BOT_RE = re.compile(r"bot|crawl|spider|slurp|facebookexternalhit|bingpreview|python|curl|wget|httpclient|"
                    r"go-http|java/|scrapy|headless|lighthouse|semrush|ahrefs|mj12|petal|bytespider|gptbot|claude",
                    re.I)
BOT_NAMES = ["Googlebot", "bingbot", "AhrefsBot", "SemrushBot", "YandexBot", "Baiduspider", "DuckDuckBot",
             "Applebot", "PetalBot", "Bytespider", "GPTBot", "ClaudeBot", "facebookexternalhit", "MJ12bot",
             "DotBot", "python", "curl", "Go-http-client", "HeadlessChrome"]
MAX_LINES = 5_000_000
CACHE_TTL = 600


def _log_files(logdir):
    files = []
    for name in os.listdir(logdir):
        m = re.match(r"^access\.log(?:\.(\d+))?(\.gz)?$", name)
        if m:
            files.append((int(m.group(1) or 0), logdir / name))
    return [p for _, p in sorted(files)]  # newest first


def _open(p):
    return gzip.open(p, "rt", errors="replace") if p.suffix == ".gz" else open(p, errors="replace")


def _bot_name(ua):
    for n in BOT_NAMES:
        if n.lower() in ua.lower():
            return n
    return "other bot"


def _ref_host(ref, own):
    if not ref or ref == "-":
        return None
    h = (urlsplit(ref).hostname or "").lower()
    if not h or h == own or h.endswith("." + own):
        return None
    return h[4:] if h.startswith("www.") else h


def compute(domain, days):
    logdir = config.HOME_ROOT / domain / "logs"
    if not logdir.is_dir():
        bad("No logs directory", 404)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    day_keys = [(since + timedelta(days=i + 1)).strftime("%Y-%m-%d") for i in range(days)]
    per_day = {d: {"hits": 0, "pageviews": 0, "bytes": 0, "visitors": set(), "bots": 0} for d in day_keys}
    paths, refs, ips, status, bots, not_found = Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
    total = lines = 0
    truncated = False
    uniques = set()
    for f in _log_files(logdir):
        older_than_range, parsed_here = True, 0
        with _open(f) as fh:
            for line in fh:
                lines += 1
                parsed_here += 1
                if lines > MAX_LINES:
                    truncated = True
                    break
                m = LINE_RE.match(line)
                if not m:
                    continue
                try:
                    ts = datetime.strptime(m.group(2), "%d/%b/%Y:%H:%M:%S %z")
                except ValueError:
                    continue
                if ts < since:
                    continue
                older_than_range = False
                day = ts.astimezone(timezone.utc).strftime("%Y-%m-%d")
                if day not in per_day:
                    continue
                ip, method, path, code, size, ref, ua = (m.group(1), m.group(3), m.group(4), int(m.group(5)),
                                                         m.group(6), m.group(7), m.group(8))
                d = per_day[day]
                total += 1
                d["hits"] += 1
                d["bytes"] += int(size) if size != "-" else 0
                status[f"{code // 100}xx"] += 1
                if BOT_RE.search(ua):
                    d["bots"] += 1
                    bots[_bot_name(ua)] += 1
                    continue
                path = path.split("?", 1)[0]
                if code == 404:
                    not_found[path] += 1
                if method == "GET" and code == 200 and not STATIC_RE.search(path) and not path.startswith("/wp-"):
                    d["pageviews"] += 1
                    d["visitors"].add(ip)
                    uniques.add(ip)
                    paths[path] += 1
                    ips[ip] += 1
                    r = _ref_host(ref, domain)
                    if r:
                        refs[r] += 1
        if truncated or (older_than_range and parsed_here):
            break  # rotated files are older — stop once a whole file is out of range
    daily = [{"day": k, "hits": v["hits"], "pageviews": v["pageviews"], "visitors": len(v["visitors"]),
              "bytes": v["bytes"], "bots": v["bots"]} for k, v in per_day.items()]
    return {
        "domain": domain, "days": days, "generated": int(time.time()), "truncated": truncated,
        "totals": {"hits": total, "pageviews": sum(x["pageviews"] for x in daily),
                   "visitors": len(uniques), "bytes": sum(x["bytes"] for x in daily),
                   "bots": sum(x["bots"] for x in daily)},
        "daily": daily, "status": dict(status),
        "top_pages": paths.most_common(15), "top_referrers": refs.most_common(10),
        "top_ips": ips.most_common(10), "bots": bots.most_common(10), "not_found": not_found.most_common(10),
    }


@router.get("/{sid}")
def site_stats(sid: int, days: int = 7, refresh: bool = False):
    s = get_site(sid)
    if days not in (1, 7, 14, 30):
        bad("days must be 1, 7, 14 or 30")
    cache_dir = config.DATA / "stats"
    cache_dir.mkdir(exist_ok=True)
    cf = cache_dir / f"{s['domain']}-{days}.json"
    if not refresh and cf.exists() and time.time() - cf.stat().st_mtime < CACHE_TTL:
        return json.loads(cf.read_text())
    data = compute(s["domain"], days)
    cf.write_text(json.dumps(data))
    return data


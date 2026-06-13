import gzip
import hashlib
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

import cloudscraper
import requests
from flask import Flask, Response, request

try:
    from curl_cffi import requests as curl_requests
    _CURL_CFFI = True
except ImportError:
    _CURL_CFFI = False

app = Flask(__name__)

# ─── TieuLam TV config ────────────────────────────────────────────────────────
TIEULAM_FRONTEND_URL  = os.environ.get("TIEULAM_FRONTEND", "https://sv1.tieulam1.live")
TIEULAM_KNOWN_API_BASE= os.environ.get("TIEULAM_API",      "https://api.tlap12062026.xyz")
# CDN phát stream — khi source_live=None, build từ stream_key
TIEULAM_STREAM_CDN    = os.environ.get("TIEULAM_CDN",      "https://live.secufun.xyz")
# Nếu IP bị chặn (Render/Vercel), dùng relay endpoint trên Replit để lấy data TieuLam
# Set TIEULAM_RELAY_URL=https://<replit-app>.replit.app/api/tieulam-relay
# Set RELAY_SECRET=<shared-secret> (tuỳ chọn, để bảo vệ endpoint)
TIEULAM_RELAY_URL    = os.environ.get("TIEULAM_RELAY_URL", "")
TIEULAM_RELAY_SECRET = os.environ.get("RELAY_SECRET", "")

# ─── Hội Quán TV config ───────────────────────────────────────────────────────
HOIQUAN_FRONTEND_URL  = os.environ.get("HOIQUAN_FRONTEND", "https://sv2.hoiquan4.live")
HOIQUAN_KNOWN_API_BASE= os.environ.get("HOIQUAN_API",      "https://sv.hoiquantv.xyz/api/v1/external")

# ─── Khán Đài A config ───────────────────────────────────────────────────────
KHANDAIA_FRONTEND_URL   = os.environ.get("KHANDAIA_FRONTEND", "https://tructiep.khandaia.link")
KHANDAIA_KNOWN_API_BASE = os.environ.get("KHANDAIA_API",      "https://sv.khandai-a.xyz/api/v1/external")

# ─── IPTV (GitHub-hosted static list) ────────────────────────────────────────
DEKIKI_M3U_URL = os.environ.get(
    "DEKIKI_M3U_URL",
    "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/vn.m3u",
)

# ─── EPG — override via env var, otherwise auto-built from /epg.xml endpoint ─
EPG_URL_OVERRIDE = os.environ.get("EPG_URL", "")

# ─── Shared config ────────────────────────────────────────────────────────────
VN_TZ                = timezone(timedelta(hours=7))
SELF_PING_INTERVAL   = 240   # seconds
PREFETCH_INTERVAL    = 300   # seconds — refresh cache every 5 minutes
API_DISCOVERY_TTL    = 3600  # seconds — re-discover API URL every 1 hour

FINISHED_STATUS_STRINGS = {"finished", "end", "ended", "complete", "completed"}
MATCH_MAX_AGE_SECONDS   = int(os.environ.get("MATCH_MAX_DURATION", 7200))  # 2 h

# ─── Sport logos (Twemoji via jsDelivr) ───────────────────────────────────────
_CDN = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72"
SPORT_LOGOS = {
    "football":   f"{_CDN}/26bd.png",
    "tennis":     f"{_CDN}/1f3be.png",
    "basketball": f"{_CDN}/1f3c0.png",
    "volleyball": f"{_CDN}/1f3d0.png",
    "billiards":  f"{_CDN}/1f3b1.png",
    "badminton":  f"{_CDN}/1f3f8.png",
    "default":    f"{_CDN}/1f3c6.png",
}

# ─── API URL caches ───────────────────────────────────────────────────────────
_tieulam_api_cache  = {"url": TIEULAM_KNOWN_API_BASE,  "discovered_at": 0}
_hoiquan_api_cache  = {"url": HOIQUAN_KNOWN_API_BASE,  "discovered_at": 0}
_khandaia_api_cache = {"url": KHANDAIA_KNOWN_API_BASE, "discovered_at": 0}

# ─── Playlist content cache ───────────────────────────────────────────────────
def _empty_entry():
    return {"content": None, "gz": None, "etag": None, "built_at": 0,
            "lock": threading.Lock()}

_playlist_cache = {
    "combined": _empty_entry(),
    "tieulam":  _empty_entry(),
    "hoiquan":  _empty_entry(),
    "khandaia": _empty_entry(),
    "dekiki":   _empty_entry(),
}

_last_counts = {
    "tieulam": 0, "hoiquan": 0, "khandaia": 0, "dekiki": 0,
    "refreshed_at": 0, "last_error": "",
}

# ─── EPG XML cache ────────────────────────────────────────────────────────────
_epg_cache: dict = {"content": None, "gz": None, "etag": None, "built_at": 0}
_epg_lock  = threading.Lock()
EPG_CACHE_TTL = 3600  # rebuild every 1 hour


# ══════════════════════════════════════════════════════════════════════════════
#  Public URL helpers
# ══════════════════════════════════════════════════════════════════════════════

def _get_public_url() -> str:
    """Return the server's public base URL (no trailing slash)."""
    # Vercel
    vercel_url = os.environ.get("VERCEL_URL", "")
    if vercel_url:
        return f"https://{vercel_url}"
    # Replit
    domains = os.environ.get("REPLIT_DOMAINS", "")
    if domains:
        return f"https://{domains.split(',')[0].strip()}"
    # Render
    render = os.environ.get("RENDER_EXTERNAL_URL", "")
    if render:
        return render.rstrip("/")
    # Manual override
    app_url = os.environ.get("APP_URL", "")
    if app_url:
        return app_url.rstrip("/")
    return f"http://localhost:{os.environ.get('PORT', 5000)}"


def _epg_url() -> str:
    if EPG_URL_OVERRIDE:
        return EPG_URL_OVERRIDE
    return f"{_get_public_url()}/epg.xml"


# ══════════════════════════════════════════════════════════════════════════════
#  EPG XML builder
# ══════════════════════════════════════════════════════════════════════════════

def _build_epg_xml() -> str:
    seen_ids:   dict[str, tuple[str, str]] = {}
    seen_names: dict[str, tuple[str, str]] = {}

    combined = _playlist_cache.get("combined", {})
    raw = combined.get("content") or b""
    content = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else (raw or "")

    for m in re.finditer(
        r'#EXTINF[^\n]*?(?:tvg-id="(?P<tid>[^"]*)")?[^\n]*?'
        r'(?:tvg-name="(?P<tname>[^"]*)")?[^\n]*?'
        r'(?:tvg-logo="(?P<tlogo>[^"]*)")?[^\n]*?,(?P<label>[^\n]*)',
        content,
    ):
        tid   = (m.group("tid")   or "").strip()
        tname = (m.group("tname") or "").strip()
        label = (m.group("label") or "").strip()
        tlogo = (m.group("tlogo") or "").strip()

        display = tname or label
        if not display:
            continue

        if tid:
            if tid not in seen_ids:
                seen_ids[tid] = (display, tlogo)
        else:
            slug = re.sub(r"[^a-z0-9]", "", display.lower())[:32]
            if slug and slug not in seen_names:
                seen_names[slug] = (display, tlogo)

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="IPTV M3U Server">']

    for cid, (name, logo) in seen_ids.items():
        esc_name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        logo_tag = f'\n    <icon src="{logo}" />' if logo else ""
        lines.append(f'  <channel id="{cid}">\n    <display-name>{esc_name}</display-name>{logo_tag}\n  </channel>')

    for slug, (name, logo) in seen_names.items():
        esc_name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        logo_tag = f'\n    <icon src="{logo}" />' if logo else ""
        lines.append(f'  <channel id="{slug}">\n    <display-name>{esc_name}</display-name>{logo_tag}\n  </channel>')

    lines.append("</tv>")
    return "\n".join(lines)


def _get_or_build_epg() -> dict:
    with _epg_lock:
        now = time.time()
        if _epg_cache["content"] is None or (now - _epg_cache["built_at"]) > EPG_CACHE_TTL:
            xml = _build_epg_xml()
            gz  = gzip.compress(xml.encode("utf-8"), compresslevel=6)
            etag = '"' + hashlib.md5(gz).hexdigest() + '"'
            _epg_cache.update({"content": xml, "gz": gz, "etag": etag, "built_at": now})
        return dict(_epg_cache)


# ══════════════════════════════════════════════════════════════════════════════
#  Sport logo helpers
# ══════════════════════════════════════════════════════════════════════════════

def _logo_from_text(text: str) -> str:
    t = text.lower()
    if "tennis" in t:
        return SPORT_LOGOS["tennis"]
    if any(k in t for k in ["basketball", "bóng rổ", "bong ro", "nba", "wnba"]):
        return SPORT_LOGOS["basketball"]
    if any(k in t for k in ["volleyball", "bóng chuyền", "bong chuyen"]):
        return SPORT_LOGOS["volleyball"]
    if any(k in t for k in ["billiard", "bi-a", "bia", "snooker", "pool", "uk open"]):
        return SPORT_LOGOS["billiards"]
    if any(k in t for k in ["badminton", "cầu lông", "cau long"]):
        return SPORT_LOGOS["badminton"]
    return SPORT_LOGOS["football"]


def _hq_kda_logo(fixture: dict) -> str:
    sport = fixture.get("sport") or {}
    icon = sport.get("iconUrl", "")
    if icon:
        return icon
    parts = " ".join([sport.get("name", ""), sport.get("slug", "")])
    return _logo_from_text(parts)


# ══════════════════════════════════════════════════════════════════════════════
#  Shared HTTP headers
# ══════════════════════════════════════════════════════════════════════════════

_HQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

_TIEULAM_HTTPX_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Content-Type": "application/json",
    "Referer": TIEULAM_FRONTEND_URL + "/",
    "Origin": TIEULAM_FRONTEND_URL,
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "cross-site",
}


# ══════════════════════════════════════════════════════════════════════════════
#  TieuLam TV — POST /matches/graph API
# ══════════════════════════════════════════════════════════════════════════════

def _discover_tieulam_api_base(scraper) -> str:
    """Quét JS bundle của frontend để tìm API base URL hiện tại (dùng cloudscraper)."""
    try:
        r = scraper.get(TIEULAM_FRONTEND_URL, timeout=10)
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        for js_path in js_files[:3]:
            js = scraper.get(
                TIEULAM_FRONTEND_URL.rstrip("/") + js_path, timeout=20
            ).text
            hits = re.findall(r'create\(\{baseURL:"(https://[^"]+)"\}', js)
            if hits:
                return hits[0].rstrip("/")
            hits = re.findall(r'baseURL:"(https://[^"]{10,60})"', js)
            if hits:
                return hits[0].rstrip("/")
    except Exception:
        pass
    return TIEULAM_KNOWN_API_BASE


def _get_tieulam_api_url(scraper=None) -> str:
    now = time.time()
    if now - _tieulam_api_cache["discovered_at"] > API_DISCOVERY_TTL:
        sc = scraper or cloudscraper.create_scraper()
        discovered = _discover_tieulam_api_base(sc)
        _tieulam_api_cache["url"] = discovered + "/matches/graph"
        _tieulam_api_cache["discovered_at"] = now
    return _tieulam_api_cache["url"]


def _fetch_tieulam_via_relay() -> list:
    """Gọi relay endpoint (bỏ qua IP block trên Render/Vercel)."""
    headers: dict = {}
    if TIEULAM_RELAY_SECRET:
        headers["X-Relay-Token"] = TIEULAM_RELAY_SECRET
    resp = requests.get(TIEULAM_RELAY_URL, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json().get("data", [])


def _fetch_tieulam_matches() -> list:
    """Fetch TieuLam matches — dùng cloudscraper để bypass Cloudflare."""
    if TIEULAM_RELAY_URL:
        try:
            return _fetch_tieulam_via_relay()
        except Exception as e:
            import sys
            print(f"⚠️ Relay failed: {e}", file=sys.stderr)

    cutoff     = (datetime.now(timezone.utc) - timedelta(seconds=MATCH_MAX_AGE_SECONDS)).strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_end = (datetime.now(timezone.utc) + timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%S")

    payload = {
        "queries": [
            {"field": "start_date", "type": "gte", "value": cutoff},
            {"field": "start_date", "type": "lte", "value": cutoff_end},
        ],
        "query_and": True,
        "limit": 100,
        "page": 1,
        "order_asc": "start_date",
    }

    # Ưu tiên curl_cffi — giả lập TLS fingerprint Chrome, bypass Cloudflare IP-block
    if _CURL_CFFI:
        try:
            api_url = _get_tieulam_api_url()
            resp = curl_requests.post(
                api_url, json=payload, headers=_TIEULAM_HTTPX_HEADERS,
                timeout=15, impersonate="chrome110",
            )
            resp.raise_for_status()
            return resp.json().get("data", [])
        except Exception:
            # Thử lại với URL discovery mới
            _tieulam_api_cache["discovered_at"] = 0
            try:
                api_url = _get_tieulam_api_url()
                resp = curl_requests.post(
                    api_url, json=payload, headers=_TIEULAM_HTTPX_HEADERS,
                    timeout=15, impersonate="chrome110",
                )
                resp.raise_for_status()
                return resp.json().get("data", [])
            except Exception:
                pass  # fallback sang cloudscraper

    # Fallback: cloudscraper (bypass JS challenge nhưng không bypass IP block)
    scraper = cloudscraper.create_scraper()
    api_url = _get_tieulam_api_url(scraper)
    try:
        resp = scraper.post(api_url, json=payload, headers=_TIEULAM_HTTPX_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception:
        _tieulam_api_cache["discovered_at"] = 0
        api_url = _get_tieulam_api_url(scraper)
        resp = scraper.post(api_url, json=payload, headers=_TIEULAM_HTTPX_HEADERS, timeout=15)
        resp.raise_for_status()

    return resp.json().get("data", [])


def _tieulam_logo(match: dict) -> str:
    logo = match.get("team_1_logo") or match.get("team_2_logo") or ""
    if logo:
        return logo
    return _logo_from_text(match.get("desc", "") + " " + match.get("league", ""))


def _build_tieulam_lines(matches: list) -> list:
    lines = []
    for match in matches:
        stream_url = (match.get("source_live") or "").strip()
        if not stream_url:
            stream_key = (match.get("stream_key") or "").strip()
            if not stream_key:
                continue
            stream_url = f"{TIEULAM_STREAM_CDN}/live/{stream_key}/playlist.m3u8"

        start_str = match.get("start_date", "")
        is_live = bool(match.get("is_live"))
        if start_str and not is_live:
            try:
                dt_start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if dt_start.tzinfo is None:
                    dt_start = dt_start.replace(tzinfo=timezone.utc)
                elapsed = time.time() - dt_start.timestamp()
                if elapsed > MATCH_MAX_AGE_SECONDS:
                    continue
            except Exception:
                pass

        logo = _tieulam_logo(match)
        team1  = match.get("team_1", "Home").strip()
        team2  = match.get("team_2", "Away").strip()
        league = match.get("league", "").strip()
        blv    = (match.get("blv") or "").strip()

        try:
            dt_start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            if dt_start.tzinfo is None:
                dt_start = dt_start.replace(tzinfo=timezone.utc)
            dt_vn    = dt_start.astimezone(VN_TZ)
            time_str = dt_vn.strftime("%H:%M")
            date_str = dt_vn.strftime("%d/%m")
        except Exception:
            time_str = "--:--"
            date_str = "--/--"

        if blv:
            display = f"{time_str} - {date_str} | {team1} VS {team2} ({league}) | {blv}"
        else:
            display = f"{time_str} - {date_str} | {team1} VS {team2} ({league})"

        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="TieuLam TV",{display}')
        lines.append(stream_url)
    return lines


# ══════════════════════════════════════════════════════════════════════════════
#  Hội Quán TV
# ══════════════════════════════════════════════════════════════════════════════

def _discover_hoiquan_api(scraper) -> str:
    try:
        r = scraper.get(HOIQUAN_FRONTEND_URL, timeout=10)
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        if not js_files:
            js_files = re.findall(r'src="(/assets/js/[^"]+\.js)"', r.text)
        if not js_files:
            return HOIQUAN_KNOWN_API_BASE
        js = scraper.get(HOIQUAN_FRONTEND_URL.rstrip("/") + js_files[0], timeout=15).text
        hits = re.findall(r'VITE_SERVER_API_BASE_URL:"(https://[^"]+)"', js)
        if hits:
            return hits[0]
        hits = re.findall(r'https://sv\.[a-z0-9\-\.]+/api/v1/external', js)
        if hits:
            return hits[0]
    except Exception:
        pass
    return HOIQUAN_KNOWN_API_BASE


def _get_hoiquan_api_base(scraper) -> str:
    now = time.time()
    if now - _hoiquan_api_cache["discovered_at"] > API_DISCOVERY_TTL:
        _hoiquan_api_cache["url"] = _discover_hoiquan_api(scraper)
        _hoiquan_api_cache["discovered_at"] = now
    return _hoiquan_api_cache["url"]


def _fetch_hoiquan_fixtures() -> list:
    scraper = cloudscraper.create_scraper()
    api_base = _get_hoiquan_api_base(scraper)
    url = api_base.rstrip("/") + "/fixtures/unfinished"
    headers = {**_HQ_HEADERS, "Referer": HOIQUAN_FRONTEND_URL + "/"}
    try:
        resp = scraper.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception:
        _hoiquan_api_cache["discovered_at"] = 0
        api_base = _get_hoiquan_api_base(scraper)
        url = api_base.rstrip("/") + "/fixtures/unfinished"
        resp = scraper.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        return []
    return data.get("data", [])


# ══════════════════════════════════════════════════════════════════════════════
#  Khán Đài A
# ══════════════════════════════════════════════════════════════════════════════

def _discover_khandaia_api(scraper) -> str:
    try:
        r = scraper.get(KHANDAIA_FRONTEND_URL, timeout=10)
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        if not js_files:
            return KHANDAIA_KNOWN_API_BASE
        for js_path in js_files:
            js = scraper.get(KHANDAIA_FRONTEND_URL.rstrip("/") + js_path, timeout=20).text
            chunk_paths = re.findall(r'assets/queries[^"\']+\.js', js)
            for cp in chunk_paths[:2]:
                chunk = scraper.get(KHANDAIA_FRONTEND_URL.rstrip("/") + "/" + cp, timeout=15).text
                hits = re.findall(r'https://sv\.[a-z0-9\-\.]+/api/v1/external', chunk)
                if hits:
                    return hits[0]
            hits = re.findall(r'https://sv\.[a-z0-9\-\.]+/api/v1/external', js)
            if hits:
                return hits[0]
    except Exception:
        pass
    return KHANDAIA_KNOWN_API_BASE


def _get_khandaia_api_base(scraper) -> str:
    now = time.time()
    if now - _khandaia_api_cache["discovered_at"] > API_DISCOVERY_TTL:
        _khandaia_api_cache["url"] = _discover_khandaia_api(scraper)
        _khandaia_api_cache["discovered_at"] = now
    return _khandaia_api_cache["url"]


def _fetch_khandaia_fixtures() -> list:
    scraper = cloudscraper.create_scraper()
    api_base = _get_khandaia_api_base(scraper)
    url = api_base.rstrip("/") + "/fixtures/unfinished"
    headers = {**_HQ_HEADERS, "Referer": KHANDAIA_FRONTEND_URL + "/"}
    try:
        resp = scraper.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception:
        _khandaia_api_cache["discovered_at"] = 0
        api_base = _get_khandaia_api_base(scraper)
        url = api_base.rstrip("/") + "/fixtures/unfinished"
        resp = scraper.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        return []
    return data.get("data", [])


# ══════════════════════════════════════════════════════════════════════════════
#  IPTV static list
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_dekiki_lines() -> list:
    resp = requests.get(DEKIKI_M3U_URL, timeout=20)
    resp.raise_for_status()
    lines = []
    for line in resp.text.splitlines():
        stripped = line.rstrip()
        if not stripped or stripped.startswith("#EXTM3U"):
            continue
        lines.append(stripped)
    return lines


# ══════════════════════════════════════════════════════════════════════════════
#  Shared fixture helpers
# ══════════════════════════════════════════════════════════════════════════════

def _fixture_is_active(fixture: dict) -> bool:
    status = str(fixture.get("status") or "").lower().strip()
    if status in FINISHED_STATUS_STRINGS:
        return False
    if fixture.get("isFinished") or fixture.get("isEnd"):
        return False
    is_live        = bool(fixture.get("isLive"))
    start_time_str = fixture.get("startTime", "")
    if start_time_str and not is_live:
        try:
            dt      = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
            elapsed = time.time() - dt.timestamp()
            if elapsed > MATCH_MAX_AGE_SECONDS:
                return False
            if status == "active" and elapsed > 5400:
                return False
        except Exception:
            pass
    return True


def _pick_best_stream(streams: list) -> str:
    for quality in ("fhd", "hd", "sd"):
        for s in streams:
            if s.get("name", "").lower() == quality:
                url = s.get("sourceUrl", "")
                if url:
                    return url
    for s in streams:
        url = s.get("sourceUrl", "")
        if url:
            return url
    return ""


def _build_fixture_lines(fixtures: list, group_title: str) -> list:
    try:
        fixtures = sorted(fixtures, key=lambda f: f.get("startTime") or "")
    except Exception:
        pass
    lines = []
    for fixture in fixtures:
        if not _fixture_is_active(fixture):
            continue
        logo      = _hq_kda_logo(fixture)
        start_str = fixture.get("startTime", "")
        home      = fixture.get("homeTeam", {}).get("name", "Home").strip()
        away      = fixture.get("awayTeam", {}).get("name", "Away").strip()
        league    = fixture.get("league", {}).get("name", "")
        try:
            dt      = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            dt_vn   = dt.astimezone(VN_TZ)
            time_str = dt_vn.strftime("%H:%M")
            date_str = dt_vn.strftime("%d/%m")
        except Exception:
            time_str = "--:--"
            date_str = "--/--"
        for entry in fixture.get("fixtureCommentators", []):
            commentator_obj = entry.get("commentator", {})
            name = (commentator_obj.get("nickname") or commentator_obj.get("name") or "").strip()
            stream_url = _pick_best_stream(commentator_obj.get("streams", []))
            if not stream_url:
                continue
            display = f"{time_str} - {date_str} | {home} VS {away} ({league}) | {name}"
            lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="{group_title}",{display}')
            lines.append(stream_url)
    return lines


# ══════════════════════════════════════════════════════════════════════════════
#  Cache helpers
# ══════════════════════════════════════════════════════════════════════════════

def _pack(text: str) -> dict:
    raw  = text.encode("utf-8")
    gz   = gzip.compress(raw, compresslevel=6)
    etag = '"' + hashlib.md5(raw).hexdigest() + '"'
    return {"content": raw, "gz": gz, "etag": etag, "built_at": time.time()}


def _store(key: str, text: str):
    packed = _pack(text)
    entry  = _playlist_cache[key]
    with entry["lock"]:
        entry.update(packed)


# ══════════════════════════════════════════════════════════════════════════════
#  Parallel fetch + rebuild
# ══════════════════════════════════════════════════════════════════════════════

def _refresh_all_playlists():
    errors = []

    def fetch_tieulam():
        return _build_tieulam_lines(_fetch_tieulam_matches())

    def fetch_hq():
        return _build_fixture_lines(_fetch_hoiquan_fixtures(), "Hội Quán TV")

    def fetch_kda():
        return _build_fixture_lines(_fetch_khandaia_fixtures(), "Khán Đài A")

    def fetch_dekiki():
        return _fetch_dekiki_lines()

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {
            ex.submit(fetch_tieulam): "tieulam",
            ex.submit(fetch_hq):      "hoiquan",
            ex.submit(fetch_kda):     "khandaia",
            ex.submit(fetch_dekiki):  "dekiki",
        }
        results = {}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                results[key] = fut.result()
            except Exception as e:
                results[key] = []
                errors.append(f"{key}: {e}")

    tieulam_lines = results.get("tieulam",  [])
    hq_lines      = results.get("hoiquan",  [])
    kda_lines     = results.get("khandaia", [])
    dekiki_lines  = results.get("dekiki",   [])

    err_str = "; ".join(errors)

    def count(lines):
        return sum(1 for l in lines if l.startswith("#EXTINF"))

    current_epg = _epg_url()
    epg_header = f'#EXTM3U url-tvg="{current_epg}" x-tvg-url="{current_epg}"'

    _store("tieulam",  epg_header + "\n" + "\n".join(tieulam_lines))
    _store("hoiquan",  epg_header + "\n" + "\n".join(hq_lines))
    _store("khandaia", epg_header + "\n" + "\n".join(kda_lines))
    _store("dekiki",   epg_header + "\n" + "\n".join(dekiki_lines))

    all_lines = tieulam_lines + hq_lines + kda_lines + dekiki_lines
    combined_text = epg_header + "\n" + "\n".join(all_lines)
    if err_str:
        combined_text += f"\n# Errors: {err_str}"
    _store("combined", combined_text)

    _last_counts.update({
        "tieulam":      count(tieulam_lines),
        "hoiquan":      count(hq_lines),
        "khandaia":     count(kda_lines),
        "dekiki":       count(dekiki_lines),
        "refreshed_at": time.time(),
        "last_error":   err_str,
    })


def _prefetch_loop():
    time.sleep(3)
    while True:
        try:
            _refresh_all_playlists()
        except Exception:
            pass
        time.sleep(PREFETCH_INTERVAL)


def _get_entry(key: str):
    entry = _playlist_cache[key]
    with entry["lock"]:
        return dict(entry)


# ══════════════════════════════════════════════════════════════════════════════
#  Flask routes
# ══════════════════════════════════════════════════════════════════════════════

def _m3u_response(key: str, filename: str) -> Response:
    entry = _get_entry(key)

    if entry["content"] is None:
        try:
            _refresh_all_playlists()
            entry = _get_entry(key)
        except Exception as e:
            return Response(f"Error: {e}", status=500, mimetype="text/plain")

    etag = entry["etag"]
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304)

    accept_enc = request.headers.get("Accept-Encoding", "")
    use_gzip   = "gzip" in accept_enc and entry["gz"] is not None

    body = entry["gz"] if use_gzip else entry["content"]

    resp = Response(body, mimetype="application/x-mpegurl")
    resp.headers["ETag"]                = etag
    resp.headers["Cache-Control"]       = f"public, max-age={PREFETCH_INTERVAL}"
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.headers["Vary"]                = "Accept-Encoding"
    if use_gzip:
        resp.headers["Content-Encoding"] = "gzip"
    return resp


@app.route("/live.m3u")
def live_m3u():
    return _m3u_response("combined", "live.m3u")


@app.route("/tieulam.m3u")
def tieulam_m3u():
    return _m3u_response("tieulam", "tieulam.m3u")


@app.route("/hoiquan.m3u")
def hoiquan_m3u():
    return _m3u_response("hoiquan", "hoiquan.m3u")


@app.route("/khandaia.m3u")
def khandaia_m3u():
    return _m3u_response("khandaia", "khandaia.m3u")


@app.route("/dekiki.m3u")
def dekiki_m3u():
    return _m3u_response("dekiki", "dekiki.m3u")


@app.route("/epg.xml")
def epg_xml():
    entry = _get_or_build_epg()

    etag = entry["etag"]
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304)

    accept_enc = request.headers.get("Accept-Encoding", "")
    use_gzip   = "gzip" in accept_enc and entry["gz"] is not None
    body = entry["gz"] if use_gzip else entry["content"].encode("utf-8")

    resp = Response(body, mimetype="application/xml; charset=utf-8")
    resp.headers["ETag"]          = etag
    resp.headers["Cache-Control"] = f"public, max-age={EPG_CACHE_TTL}"
    resp.headers["Vary"]          = "Accept-Encoding"
    if use_gzip:
        resp.headers["Content-Encoding"] = "gzip"
    return resp


@app.route("/ping")
def ping():
    return Response("OK", mimetype="text/plain")


@app.route("/api/tieulam-relay")
def tieulam_relay():
    """Relay endpoint — nhận request từ Render/Vercel instance khác bị block IP."""
    secret = os.environ.get("RELAY_SECRET", "")
    if secret:
        token = request.headers.get("X-Relay-Token", "")
        if token != secret:
            return Response("Unauthorized", status=401, mimetype="text/plain")
    try:
        data = _fetch_tieulam_matches()
        return {"data": data}
    except Exception as e:
        return Response(f"Error: {e}", status=500, mimetype="text/plain")


@app.route("/")
def index():
    ra = _last_counts.get("refreshed_at", 0)
    if ra:
        dt_str   = datetime.fromtimestamp(ra, tz=VN_TZ).strftime("%H:%M:%S %d/%m/%Y")
        next_s   = max(int(PREFETCH_INTERVAL - (time.time() - ra)), 0)
        next_str = f"{next_s}s"
    else:
        dt_str   = "chưa có dữ liệu"
        next_str = "đang khởi động..."

    err      = _last_counts.get("last_error", "")
    err_html = f'<p style="color:red">⚠️ {err}</p>' if err else ""

    tieulam_count = _last_counts.get("tieulam", 0)
    hq_count      = _last_counts.get("hoiquan", 0)
    kda_count     = _last_counts.get("khandaia", 0)
    dekiki_count  = _last_counts.get("dekiki", 0)
    total         = tieulam_count + hq_count + kda_count + dekiki_count

    epg_link = _epg_url()
    return (
        "<h2>🎬 IPTV M3U Server</h2>"
        "<h3>📋 Playlist</h3><ul>"
        "<li><a href='/live.m3u'>/live.m3u</a> — Tất cả nguồn gộp lại</li>"
        "<li><a href='/tieulam.m3u'>/tieulam.m3u</a> — TieuLam TV only</li>"
        "<li><a href='/hoiquan.m3u'>/hoiquan.m3u</a> — Hội Quán TV only</li>"
        "<li><a href='/khandaia.m3u'>/khandaia.m3u</a> — Khán Đài A only</li>"
        "<li><a href='/dekiki.m3u'>/dekiki.m3u</a> — Kênh TV Việt (IPTV)</li>"
        "</ul>"
        "<h3>📡 EPG</h3><ul>"
        f"<li><a href='/epg.xml'>/epg.xml</a> — XMLTV tự sinh từ danh sách kênh (cache 1h)</li>"
        f"<li>URL đầy đủ: <code>{epg_link}</code></li>"
        "</ul>"
        "<h3>📊 Trạng thái</h3>"
        f"<p>📺 Tổng kênh: <strong>{total}</strong>"
        f" &nbsp;(🏆 Live: {tieulam_count + hq_count + kda_count}"
        f" | 📡 TV: {dekiki_count})</p>"
        f"<p>🕐 Cập nhật lần cuối: <strong>{dt_str}</strong></p>"
        f"<p>⏳ Cập nhật tiếp theo: <strong>{next_str}</strong></p>"
        f"<p>🟢 TieuLam TV: <strong>{tieulam_count} kênh</strong>"
        f"&nbsp;|&nbsp; <code>{_tieulam_api_cache['url']}</code></p>"
        f"<p>🟢 Hội Quán TV: <strong>{hq_count} kênh</strong>"
        f"&nbsp;|&nbsp; <code>{_hoiquan_api_cache['url']}</code></p>"
        f"<p>🟢 Khán Đài A: <strong>{kda_count} kênh</strong>"
        f"&nbsp;|&nbsp; <code>{_khandaia_api_cache['url']}</code></p>"
        f"<p>📡 Kênh TV (IPTV): <strong>{dekiki_count} kênh</strong></p>"
        f"{err_html}"
        "<h3>⚙️ Tối ưu băng thông</h3>"
        "<ul>"
        "<li>Gzip nén tự động (giảm ~70% dữ liệu truyền)</li>"
        "<li>ETag + HTTP 304 — client có sẵn cache không cần tải lại</li>"
        f"<li>Cache-Control: public, max-age={PREFETCH_INTERVAL}s</li>"
        "<li>Fetch 4 nguồn song song (ThreadPoolExecutor)</li>"
        f"<li>Làm mới cache mỗi <strong>{PREFETCH_INTERVAL // 60} phút</strong></li>"
        "</ul>"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Keep-alive self-ping (chỉ dùng khi chạy server thường, không dùng trên Vercel)
# ══════════════════════════════════════════════════════════════════════════════

def _get_ping_url() -> str:
    domains = os.environ.get("REPLIT_DOMAINS", "")
    if domains:
        return f"https://{domains.split(',')[0].strip()}/"
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if render_url:
        return render_url.rstrip("/") + "/"
    app_url = os.environ.get("APP_URL", "")
    if app_url:
        return app_url.rstrip("/") + "/"
    return f"http://localhost:{os.environ.get('PORT', 5000)}/"


def _self_ping():
    url = _get_ping_url()
    while True:
        time.sleep(SELF_PING_INTERVAL)
        try:
            requests.get(url, timeout=15)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  Startup — chỉ khi chạy trực tiếp (không phải Vercel serverless)
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    threading.Thread(target=_prefetch_loop, daemon=True).start()
    threading.Thread(target=_self_ping,     daemon=True).start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

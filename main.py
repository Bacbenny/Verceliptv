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
TIEULAM_STREAM_CDN    = os.environ.get("TIEULAM_CDN",      "https://live.secufun.xyz")
TIEULAM_RELAY_URL     = os.environ.get("TIEULAM_RELAY_URL", "")
TIEULAM_RELAY_SECRET  = os.environ.get("RELAY_SECRET", "")

# ─── Hội Quán TV config ───────────────────────────────────────────────────────
HOIQUAN_FRONTEND_URL  = os.environ.get("HOIQUAN_FRONTEND", "https://sv2.hoiquan4.live")
HOIQUAN_KNOWN_API_BASE= os.environ.get("HOIQUAN_API",      "https://sv.hoiquantv.xyz/api/v1/external")

# ─── Khán Đài A config ───────────────────────────────────────────────────────
KHANDAIA_FRONTEND_URL   = os.environ.get("KHANDAIA_FRONTEND", "https://tructiep.khandaia.link")
KHANDAIA_KNOWN_API_BASE = os.environ.get("KHANDAIA_API",      "https://sv.khandai-a.xyz/api/v1/external")

# ─── EPG ─────────────────────────────────────────────────────────────────────
EPG_URL_OVERRIDE = os.environ.get("EPG_URL", "")

# ─── Shared config ────────────────────────────────────────────────────────────
VN_TZ                = timezone(timedelta(hours=7))
SELF_PING_INTERVAL   = 240
PREFETCH_INTERVAL    = 300
API_DISCOVERY_TTL    = 3600

FINISHED_STATUS_STRINGS = {"finished", "end", "ended", "complete", "completed"}
MATCH_MAX_AGE_SECONDS   = int(os.environ.get("MATCH_MAX_DURATION", 7200))

# ─── Sport logos ──────────────────────────────────────────────────────────────
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
}

_last_counts = {
    "tieulam": 0, "hoiquan": 0, "khandaia": 0,
    "refreshed_at": 0, "last_error": "",
}

# ─── EPG XML cache ────────────────────────────────────────────────────────────
_epg_cache: dict = {"content": None, "gz": None, "etag": None, "built_at": 0}
_epg_lock  = threading.Lock()
EPG_CACHE_TTL = 3600


# ══════════════════════════════════════════════════════════════════════════════
#  Public URL helpers
# ══════════════════════════════════════════════════════════════════════════════

def _get_public_url() -> str:
    vercel_url = os.environ.get("VERCEL_URL", "")
    if vercel_url:
        return f"https://{vercel_url}"
    domains = os.environ.get("REPLIT_DOMAINS", "")
    if domains:
        return f"https://{domains.split(',')[0].strip()}"
    render = os.environ.get("RENDER_EXTERNAL_URL", "")
    if render:
        return render.rstrip("/")
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
#  TieuLam TV
# ══════════════════════════════════════════════════════════════════════════════

def _discover_tieulam_api_base(scraper) -> str:
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
    headers: dict = {}
    if TIEULAM_RELAY_SECRET:
        headers["X-Relay-Token"] = TIEULAM_RELAY_SECRET
    resp = requests.get(TIEULAM_RELAY_URL, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json().get("data", [])


def _fetch_tieulam_matches() -> list:
    if TIEULAM_RELAY_URL:
        try:
            return _fetch_tieulam_via_relay()
        except Exception as e:
            import sys
            print(f"Relay failed: {e}", file=sys.stderr)

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
                pass

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


_TIEULAM_SPORT_VI = {
    "FOOTBALL":    ("Bong da",   SPORT_LOGOS["football"]),
    "VOLLEYBALL":  ("Bong chuyen", SPORT_LOGOS["volleyball"]),
    "BASKETBALL":  ("Bong ro",   SPORT_LOGOS["basketball"]),
    "TENNIS":      ("Quan vot",  SPORT_LOGOS["tennis"]),
    "BADMINTON":   ("Cau long",  SPORT_LOGOS["badminton"]),
    "BILLIARD":    ("Bi-a",      SPORT_LOGOS["billiards"]),
    "SNOOKER":     ("Snooker",   SPORT_LOGOS["billiards"]),
}


def _tieulam_logo(match: dict) -> str:
    desc = (match.get("desc") or "").upper()
    sport_info = _TIEULAM_SPORT_VI.get(desc)
    if sport_info:
        return sport_info[1]
    return _logo_from_text(desc + " " + match.get("league", ""))


def _tieulam_sport_label(match: dict) -> str:
    desc = (match.get("desc") or "").upper()
    sport_info = _TIEULAM_SPORT_VI.get(desc)
    if sport_info:
        return sport_info[0]
    if desc:
        return desc.capitalize()
    return ""


def _parse_iso_to_ts(s: str) -> float:
    """Parse ISO-8601 string to UTC timestamp float; returns inf on failure."""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return float("inf")


# ══════════════════════════════════════════════════════════════════════════════
#  Entry builders — return (sort_ts, extinf_line, url_line) tuples
# ══════════════════════════════════════════════════════════════════════════════

def _build_tieulam_entries(matches: list) -> list[tuple[float, str, str]]:
    """
    Returns list of (utc_timestamp, extinf_line, url_line).
    Sorted by start_date so that single-source playlists are also ordered.
    """
    # Sort by start_date ascending before processing
    try:
        matches = sorted(matches, key=lambda m: m.get("start_date") or "")
    except Exception:
        pass

    entries: list[tuple[float, str, str]] = []
    for match in matches:
        source_live = (match.get("source_live") or "").strip()
        blv         = (match.get("blv") or "").strip()
        stream_key  = (match.get("stream_key") or "").strip()
        is_live     = bool(match.get("is_live"))

        if source_live:
            # Đang live, có CDN URL xác nhận
            stream_url = source_live
        elif stream_key:
            # Có stream_key — xây URL từ CDN (bao gồm cả trận có/không có BLV)
            stream_url = f"{TIEULAM_STREAM_CDN}/live/{stream_key}/playlist.m3u8"
        else:
            continue

        start_str = match.get("start_date", "")
        sort_ts   = _parse_iso_to_ts(start_str) if start_str else float("inf")

        if start_str and not is_live:
            elapsed = time.time() - sort_ts
            if blv:
                # BLV: hiển thị từ 12h trước đến hết MATCH_MAX_AGE_SECONDS sau giờ bắt đầu
                if elapsed < -43200:
                    continue
            elif source_live:
                # source_live không cần check elapsed — CDN đang phát
                pass
            else:
                # Chỉ có stream_key: hiển thị từ 30 phút trước khi bắt đầu
                if elapsed < -1800:
                    continue
            if elapsed > MATCH_MAX_AGE_SECONDS:
                continue

        logo   = _tieulam_logo(match)
        team1  = match.get("team_1", "Home").strip()
        team2  = match.get("team_2", "Away").strip()
        league = match.get("league", "").strip()
        sport  = _tieulam_sport_label(match)

        try:
            dt_vn    = datetime.fromtimestamp(sort_ts, tz=VN_TZ)
            time_str = dt_vn.strftime("%H:%M")
            date_str = dt_vn.strftime("%d/%m")
        except Exception:
            time_str = "--:--"
            date_str = "--/--"

        suffix = blv if blv else sport
        if suffix:
            display = f"{time_str} - {date_str} | {team1} VS {team2} ({league}) | {suffix}"
        else:
            display = f"{time_str} - {date_str} | {team1} VS {team2} ({league})"

        extinf = f'#EXTINF:-1 tvg-logo="{logo}" group-title="TieuLam TV",{display}'
        entries.append((sort_ts, extinf, stream_url))

    return entries


def _build_fixture_entries(fixtures: list, group_title: str) -> list[tuple[float, str, str]]:
    """
    Returns list of (utc_timestamp, extinf_line, url_line).
    Sorted by startTime so that single-source playlists are also ordered.
    """
    try:
        fixtures = sorted(fixtures, key=lambda f: f.get("startTime") or "")
    except Exception:
        pass

    entries: list[tuple[float, str, str]] = []
    for fixture in fixtures:
        if not _fixture_is_active(fixture):
            continue

        logo      = _hq_kda_logo(fixture)
        start_str = fixture.get("startTime", "")
        home      = fixture.get("homeTeam", {}).get("name", "Home").strip()
        away      = fixture.get("awayTeam", {}).get("name", "Away").strip()
        league    = fixture.get("league", {}).get("name", "")

        sort_ts = _parse_iso_to_ts(start_str) if start_str else float("inf")

        try:
            dt_vn    = datetime.fromtimestamp(sort_ts, tz=VN_TZ)
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
            extinf  = f'#EXTINF:-1 tvg-logo="{logo}" group-title="{group_title}",{display}'
            entries.append((sort_ts, extinf, stream_url))

    return entries


def _entries_to_lines(entries: list[tuple[float, str, str]]) -> list[str]:
    lines: list[str] = []
    for _, extinf, url in entries:
        lines.append(extinf)
        lines.append(url)
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
    scraper  = cloudscraper.create_scraper()
    api_base = _get_hoiquan_api_base(scraper)
    url      = api_base.rstrip("/") + "/fixtures/unfinished"
    headers  = {**_HQ_HEADERS, "Referer": HOIQUAN_FRONTEND_URL + "/"}
    try:
        resp = scraper.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception:
        _hoiquan_api_cache["discovered_at"] = 0
        api_base = _get_hoiquan_api_base(scraper)
        url  = api_base.rstrip("/") + "/fixtures/unfinished"
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
                hits  = re.findall(r'https://sv\.[a-z0-9\-\.]+/api/v1/external', chunk)
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
    scraper  = cloudscraper.create_scraper()
    api_base = _get_khandaia_api_base(scraper)
    url      = api_base.rstrip("/") + "/fixtures/unfinished"
    headers  = {**_HQ_HEADERS, "Referer": KHANDAIA_FRONTEND_URL + "/"}
    try:
        resp = scraper.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception:
        _khandaia_api_cache["discovered_at"] = 0
        api_base = _get_khandaia_api_base(scraper)
        url  = api_base.rstrip("/") + "/fixtures/unfinished"
        resp = scraper.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        return []
    return data.get("data", [])


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
        return _build_tieulam_entries(_fetch_tieulam_matches())

    def fetch_hq():
        return _build_fixture_entries(_fetch_hoiquan_fixtures(), "Hoi Quan TV")

    def fetch_kda():
        return _build_fixture_entries(_fetch_khandaia_fixtures(), "Khan Dai A")

    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
            ex.submit(fetch_tieulam): "tieulam",
            ex.submit(fetch_hq):      "hoiquan",
            ex.submit(fetch_kda):     "khandaia",
        }
        results = {}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                results[key] = fut.result()
            except Exception as e:
                results[key] = []
                errors.append(f"{key}: {e}")

    tieulam_entries = results.get("tieulam",  [])
    hq_entries      = results.get("hoiquan",  [])
    kda_entries     = results.get("khandaia", [])

    err_str = "; ".join(errors)

    current_epg = _epg_url()
    epg_header  = f'#EXTM3U url-tvg="{current_epg}" x-tvg-url="{current_epg}"'

    _store("tieulam",  epg_header + "\n" + "\n".join(_entries_to_lines(tieulam_entries)))
    _store("hoiquan",  epg_header + "\n" + "\n".join(_entries_to_lines(hq_entries)))
    _store("khandaia", epg_header + "\n" + "\n".join(_entries_to_lines(kda_entries)))

    # Merge all entries and sort by start time — ensures combined playlist is chronological
    all_entries = tieulam_entries + hq_entries + kda_entries
    all_entries.sort(key=lambda e: e[0])  # sort by UTC timestamp

    all_lines    = _entries_to_lines(all_entries)
    combined_text = epg_header + "\n" + "\n".join(all_lines)
    if err_str:
        combined_text += f"\n# Errors: {err_str}"
    _store("combined", combined_text)

    def count(entries):
        return len(entries)

    _last_counts.update({
        "tieulam":      count(tieulam_entries),
        "hoiquan":      count(hq_entries),
        "khandaia":     count(kda_entries),
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
        dt_str   = "chua co du lieu"
        next_str = "dang khoi dong..."

    err      = _last_counts.get("last_error", "")
    err_html = f'<p style="color:red">Loi: {err}</p>' if err else ""

    tieulam_count = _last_counts.get("tieulam", 0)
    hq_count      = _last_counts.get("hoiquan", 0)
    kda_count     = _last_counts.get("khandaia", 0)
    total         = tieulam_count + hq_count + kda_count

    epg_link = _epg_url()
    return (
        "<h2>IPTV M3U Server</h2>"
        "<h3>Playlist</h3><ul>"
        "<li><a href='/live.m3u'>/live.m3u</a> — Tat ca nguon gop lai (sap xep theo gio)</li>"
        "<li><a href='/tieulam.m3u'>/tieulam.m3u</a> — TieuLam TV only</li>"
        "<li><a href='/hoiquan.m3u'>/hoiquan.m3u</a> — Hoi Quan TV only</li>"
        "<li><a href='/khandaia.m3u'>/khandaia.m3u</a> — Khan Dai A only</li>"
        "</ul>"
        "<h3>EPG</h3><ul>"
        f"<li><a href='/epg.xml'>/epg.xml</a></li>"
        f"<li>URL: <code>{epg_link}</code></li>"
        "</ul>"
        "<h3>Trang thai</h3>"
        f"<p>Tong kenh live: <strong>{total}</strong> "
        f"(TieuLam: {tieulam_count}, HoiQuan: {hq_count}, KhanDaiA: {kda_count})</p>"
        f"<p>Cap nhat lan cuoi: <strong>{dt_str}</strong> (lam moi sau: {next_str})</p>"
        + err_html +
        "<h3>Relay</h3>"
        f"<p><a href='/api/tieulam-relay'>/api/tieulam-relay</a></p>"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Background threads
# ══════════════════════════════════════════════════════════════════════════════

def _self_ping_loop():
    time.sleep(30)
    pub = _get_public_url()
    while True:
        try:
            requests.get(f"{pub}/ping", timeout=10)
        except Exception:
            pass
        time.sleep(SELF_PING_INTERVAL)


threading.Thread(target=_prefetch_loop, daemon=True).start()
threading.Thread(target=_self_ping_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

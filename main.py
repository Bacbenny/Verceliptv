import gzip
import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import cloudscraper
import requests
from flask import Flask, Response, request, redirect

app = Flask(__name__)

_PLAYLIST_CACHE_CONTROL = "no-store, no-cache, must-revalidate, max-age=0, private"


@app.after_request
def _disable_playlist_caching(response):
    """Prevent browsers, IPTV clients, and edge caches from reusing playlists."""
    if request.path.endswith((".m3u", ".json")):
        response.headers["Cache-Control"] = _PLAYLIST_CACHE_CONTROL
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["Surrogate-Control"] = "no-store"
        response.headers["CDN-Cache-Control"] = "no-store"
    return response

# ─── Shared HTTP sessions (connection reuse) ───────────────────────────────────
# Reusing TCP+TLS connections across calls to the same host saves
# 200-500ms per request under load.
_http_session = requests.Session()
_http_session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

# ─── Cola TV config ───────────────────────────────────────────────────────────
COLATV_FRONTEND_URL   = os.environ.get("COLATV_FRONTEND", "https://colatv48.live")
COLATV_KNOWN_API_URL  = os.environ.get("COLATV_API",      "https://api.cltvlv.com/api/matches")

# ─── Pháo Hoa TV config ──────────────────────────────────────────────────────
PHAOHOA_FRONTEND_URL   = os.environ.get("PHAOHOA_FRONTEND", "https://khandai3.link")
PHAOHOA_API_URL        = os.environ.get("PHAOHOA_API",      "https://khandai3.link/api/matches/")
PHAOHOA_FETCH_URL      = "https://khandai3.link/api/matches/?ordering=-start_time&page_size=100"

if "phaohoa1.live" in PHAOHOA_FRONTEND_URL:
    PHAOHOA_FRONTEND_URL = "https://khandai3.link"
if "phaohoa1.live" in PHAOHOA_API_URL:
    PHAOHOA_API_URL = "https://khandai3.link/api/matches/"

# ─── PhaLang TV config ───────────────────────────────────────────────────────
PHALANG_FRONTEND_URL = os.environ.get("PHALANG_FRONTEND", "https://phalang.tv")
PHALANG_API_URL      = os.environ.get("PHALANG_API", "https://api.plapi202624081158.com")

# ─── Giờ Vàng TV config ──────────────────────────────────────────────────────
GIOVANG_FRONTEND_URL = os.environ.get("GIOVANG_FRONTEND", "https://giovang.asia")
GIOVANG_API_HOST     = os.environ.get(
    "GIOVANG_API_HOST", "https://live-api.keonhacaitp.one"
).rstrip("/")

# ─── Dekiki (GitHub-hosted static list) + EPG ────────────────────────────────
DEKIKI_M3U_URL = os.environ.get(
    "DEKIKI_M3U_URL",
    "https://raw.githubusercontent.com/Bacbenny/Verceliptv/refs/heads/main/dekiki",
)
EPG_URL = os.environ.get("EPG_URL", "https://lichphatsong.io.vn/epg.xml")

# ─── Shared config ────────────────────────────────────────────────────────────
VN_TZ                = timezone(timedelta(hours=7))
SELF_PING_INTERVAL   = 240   # seconds
PREFETCH_INTERVAL    = 300   # seconds — refresh cache every 5 minutes
API_DISCOVERY_TTL    = 3600  # seconds — re-discover API URL every 1 hour

COLATV_FINISHED_STATUS_INT = {3}
FINISHED_STATUS_STRINGS    = {"finished", "end", "ended", "complete", "completed"}
MATCH_MAX_AGE_SECONDS      = int(os.environ.get("MATCH_MAX_DURATION", 7200))  # 2 h

# ─── Sport logos (Twemoji via jsDelivr) ───────────────────────────────────────
_CDN = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72"
SPORT_LOGOS = {
    "football":    f"{_CDN}/26bd.png",
    "tennis":      f"{_CDN}/1f3be.png",
    "basketball":  f"{_CDN}/1f3c0.png",
    "volleyball":  f"{_CDN}/1f3d0.png",
    "billiards":   f"{_CDN}/1f3b1.png",
    "badminton":   f"{_CDN}/1f3f8.png",
    "boxing":      f"{_CDN}/1f94a.png",
    "golf":        f"{_CDN}/26f3.png",
    "esport":      f"{_CDN}/1f3ae.png",
    "motorsport":  f"{_CDN}/1f3ce.png",
    "athletics":   f"{_CDN}/1f3c3.png",
    "swimming":    f"{_CDN}/1f3ca.png",
    "martialarts": f"{_CDN}/1f94b.png",
    "cycling":     f"{_CDN}/1f6b4.png",
    "hockey":      f"{_CDN}/1f3d2.png",
    "default":     f"{_CDN}/1f3c6.png",
}


# ─── API URL caches ───────────────────────────────────────────────────────────
_colatv_api_cache   = {"url": COLATV_KNOWN_API_URL,    "discovered_at": 0}
_phaohoa_api_cache  = {"url": PHAOHOA_API_URL,  "discovered_at": 0}
_giovang_api_cache  = {"host": GIOVANG_API_HOST, "discovered_at": 0}
_phalang_api_cache  = {"url": PHALANG_API_URL,   "discovered_at": 0}

# ─── Auto domain resolution ───────────────────────────────────────────────────
def _resolve_base_url(url: str, timeout: int = 8) -> str:
    try:
        r = _http_session.get(
            url, timeout=timeout, allow_redirects=True,
        )
        final = r.url or url
    except Exception:
        final = url
    m = re.match(r"(https?://[^/?#]+)", final)
    return m.group(1) if m else url.rstrip("/")


def _resolve_all_frontends() -> None:
    global PHAOHOA_FRONTEND_URL
    sources = {
        "Pháo Hoa TV": ("PHAOHOA", PHAOHOA_FRONTEND_URL),
    }
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = {pool.submit(_resolve_base_url, cfg[1]): (name, cfg) for name, cfg in sources.items()}
        for fut in as_completed(futures):
            (name, (key, original)) = futures[fut]
            try:
                resolved = fut.result()
            except Exception:
                resolved = original
            if resolved != original.rstrip("/"):
                print(f"[domain-resolve] {name}: {original} → {resolved}", flush=True)
            if key == "PHAOHOA":
                PHAOHOA_FRONTEND_URL = resolved


# ─── Playlist content cache ───────────────────────────────────────────────────
def _empty_entry():
    return {"content": None, "gz": None, "etag": None, "built_at": 0,
            "lock": threading.Lock()}

_playlist_cache = {
    "combined": _empty_entry(),
    "cola":     _empty_entry(),
    "phaohoa":  _empty_entry(),
    "giovang":  _empty_entry(),
    "phalang":  _empty_entry(),
    "dekiki":   _empty_entry(),
}

_last_counts = {
    "cola": 0, "phaohoa": 0, "giovang": 0, "phalang": 0, "dekiki": 0,
    "refreshed_at": 0, "last_error": "",
}

_background_lock = threading.Lock()
_background_started = False

_refresh_lock = threading.Lock()
_refresh_in_progress = False

_source_refresh_locks = {
    key: threading.Lock()
    for key in ("cola", "phaohoa", "giovang", "phalang", "dekiki")
}
_source_timing_lock = threading.Lock()
_source_refresh_ms = {}
_source_refresh_errors = {}

_upcoming_cache: dict[str, dict] = {}
_upcoming_cache_ttl = 60
_upcoming_cache_lock = threading.Lock()
_UPCOMING_CACHE_MAX = 200


def _ensure_background_tasks() -> None:
    global _background_started
    if _background_started:
        return
    with _background_lock:
        if _background_started:
            return
        threading.Thread(target=_prefetch_loop, daemon=True, name="playlist-refresh").start()
        threading.Thread(target=_self_ping, daemon=True, name="self-ping").start()
        _background_started = True

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
    if any(k in t for k in ["boxing", "kickbox", "muay", "quyền anh", "quyen anh", "ufc", "mma"]):
        return SPORT_LOGOS["boxing"]
    if any(k in t for k in ["golf"]):
        return SPORT_LOGOS["golf"]
    if any(k in t for k in ["esport", "e-sport", "gaming", "lol", "dota", "valorant", "fifa online"]):
        return SPORT_LOGOS["esport"]
    if any(k in t for k in ["formula", "f1 ", " f1", "motogp", "moto gp", "đua xe", "dua xe", "motorsport", "superbike", "wtcc"]):
        return SPORT_LOGOS["motorsport"]
    if any(k in t for k in ["athletics", "điền kinh", "dien kinh", "marathon", "chạy", "cha y"]):
        return SPORT_LOGOS["athletics"]
    if any(k in t for k in ["swim", "bơi lội", "boi loi", "aquatic"]):
        return SPORT_LOGOS["swimming"]
    if any(k in t for k in ["karate", "judo", "taekwondo", "wushu", "võ thuật", "vo thuat",
                              "wrestling", "kung fu", "wwe", "smackdown", "raw", "aew",
                              "impact", "muay thai", "kickboxing", "bjj"]):
        return SPORT_LOGOS["martialarts"]
    if any(k in t for k in ["cycl", "xe đạp", "xe dap", "velo"]):
        return SPORT_LOGOS["cycling"]
    if any(k in t for k in ["hockey", "khúc côn", "khuc con"]):
        return SPORT_LOGOS["hockey"]
    return SPORT_LOGOS["football"]

def _cola_logo(match: dict) -> str:
    parts = " ".join([
        match.get("competitionName", ""),
        match.get("sportType", ""),
        match.get("sport", ""),
        str(match.get("sportId", "")),
    ])
    return _logo_from_text(parts)

def _hq_kda_logo(fixture: dict) -> str:
    sport = fixture.get("sport") or {}
    icon = sport.get("iconUrl", "")
    if icon:
        return icon
    parts = " ".join([sport.get("name", ""), sport.get("slug", "")])
    return _logo_from_text(parts)

# ══════════════════════════════════════════════════════════════════════════════
#  Cola TV — API discovery + fetch
# ══════════════════════════════════════════════════════════════════════════════

def _discover_colatv_api(scraper) -> str:
    try:
        r = scraper.get(COLATV_FRONTEND_URL, timeout=10)
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        if not js_files:
            return COLATV_KNOWN_API_URL
        js = scraper.get(COLATV_FRONTEND_URL.rstrip("/") + js_files[0], timeout=15).text
        hits = re.findall(r'https://[a-z0-9\-\.]+/api/match[^"\'`\s]{0,30}', js)
        for hit in hits:
            base = re.match(r'(https://[a-z0-9\-\.]+)/api/', hit)
            if base:
                return base.group(1) + "/api/matches"
    except Exception:
        pass
    return COLATV_KNOWN_API_URL

def _get_colatv_api_url(scraper) -> str:
    now = time.time()
    if now - _colatv_api_cache["discovered_at"] > API_DISCOVERY_TTL:
        _colatv_api_cache["url"] = _discover_colatv_api(scraper)
        _colatv_api_cache["discovered_at"] = now
    return _colatv_api_cache["url"]

def _fetch_colatv_matches() -> dict:
    scraper = cloudscraper.create_scraper()
    api_url = _get_colatv_api_url(scraper)
    errors = []

    # CoLa occasionally times out during a cold API connection. Retry the
    # current URL before rediscovering it, so a transient timeout does not
    # make the IPTV client fall back to an older local playlist.
    for attempt in range(3):
        try:
            resp = scraper.get(api_url, timeout=10)
            resp.raise_for_status()
            payload = resp.json()
            data = payload.get("data", {})
            if not isinstance(data, dict):
                raise RuntimeError("CoLa API returned an invalid data object")
            return data
        except Exception as exc:
            errors.append(f"attempt {attempt + 1}: {type(exc).__name__}: {exc}")
            _colatv_api_cache["discovered_at"] = 0
            if attempt < 2:
                try:
                    api_url = _get_colatv_api_url(scraper)
                except Exception:
                    # Keep the last known URL for the next retry.
                    pass

    raise RuntimeError("CoLa API failed after 3 attempts: " + " | ".join(errors)[-700:])

def _colatv_has_stream(match: dict) -> bool:
    """Treat an available stream as live evidence when the schedule lags."""
    anchors = match.get("anchorAppointmentVoList") or []
    if any(
        str(anchor.get("playStreamAddress2") or anchor.get("playStreamAddress") or "").strip()
        for anchor in anchors
        if isinstance(anchor, dict)
    ):
        return True
    return bool(str(match.get("videoUrl") or "").strip())


def _colatv_is_active(match: dict) -> bool:
    if match.get("matchStatus") in COLATV_FINISHED_STATUS_INT:
        return False
    for field in ("match_status", "status", "matchStatusStr"):
        if str(match.get(field, "")).lower().strip() in FINISHED_STATUS_STRINGS:
            return False
    if match.get("isEnd") or match.get("isFinished"):
        return False

    match_time = match.get("matchTime", 0)
    is_live = bool(match.get("isLive") or match.get("living"))
    has_stream = _colatv_has_stream(match)
    if match_time and not is_live and not has_stream:
        if (time.time() - match_time) > MATCH_MAX_AGE_SECONDS:
            return False
    return True

def _build_colatv_lines(matches: dict) -> list:
    lines = []
    for match in matches.values():
        if not _colatv_is_active(match):
            continue
        logo        = _cola_logo(match)
        match_time  = match.get("matchTime", 0)
        home        = match.get("homeTeamName", "Home")
        away        = match.get("awayTeamName", "Away")
        competition = match.get("competitionName", "")
        dt          = datetime.fromtimestamp(match_time, tz=VN_TZ)
        time_str    = dt.strftime("%H:%M")
        date_str    = dt.strftime("%d/%m")
        anchors = match.get("anchorAppointmentVoList", [])
        if anchors:
            for anchor in anchors:
                stream_url = anchor.get("playStreamAddress2") or anchor.get("playStreamAddress", "")
                if not stream_url:
                    continue
                commentator = anchor.get("nickName", "").strip()
                display = f"{time_str} - {date_str} | {home} VS {away} ({competition}) | {commentator}"
                lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="CoLa TV",{display}')
                lines.append(stream_url)
        else:
            stream_url = match.get("videoUrl", "")
            if not stream_url:
                continue
            display = f"{time_str} - {date_str} | {home} VS {away} ({competition})"
            lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="CoLa TV",{display}')
            lines.append(stream_url)
    return lines

# ══════════════════════════════════════════════════════════════════════════════
#  Pháo Hoa TV — fetch từ khandai3.link/api/matches (Django REST, không token)
# ══════════════════════════════════════════════════════════════════════════════

_PHAOHOA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://khandai3.link/",
    "Accept": "application/json",
}


def _json_from_reader(text: str) -> dict:
    start = text.find("{")
    if start < 0:
        raise RuntimeError("Pháo Hoa proxy returned no JSON object")
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise RuntimeError("Pháo Hoa proxy returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Pháo Hoa proxy returned a non-object response")
    return data


def _fetch_phaohoa_json(url: str) -> dict:
    try:
        resp = _http_session.get(url, headers=_PHAOHOA_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError("Pháo Hoa API returned a non-object response")
        return data
    except Exception as direct_error:
        reader_target = url.replace("https://", "http://", 1) if url.startswith("https://") else url
        format_sep = "&" if "?" in reader_target else "?"
        reader_target += format_sep + "format=json"
        reader_url = "https://r.jina.ai/" + reader_target.replace("&", "%26")
        try:
            proxy_resp = _http_session.get(
                reader_url,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "text/plain"},
                timeout=45,
            )
            proxy_resp.raise_for_status()
            return _json_from_reader(proxy_resp.text)
        except Exception as proxy_error:
            raise RuntimeError(
                f"direct API failed ({direct_error}); Reader fallback failed ({proxy_error})"
            ) from proxy_error

def _fetch_phaohoa_matches() -> list:
    """Fetch Pháo Hoa matches — first page synchronously, remaining pages in parallel."""
    url = PHAOHOA_FETCH_URL
    data = _fetch_phaohoa_json(url)
    page_results = data.get("results", [])
    if not isinstance(page_results, list):
        raise RuntimeError("Pháo Hoa API returned an invalid results list")
    results = [m for m in page_results if isinstance(m, dict)]

    # Collect remaining page URLs from the first page's "next" chain.
    # The Django REST API exposes page numbers in the URL, so we can
    # build all remaining URLs from the first "next" URL without fetching
    # each page sequentially.
    next_url = data.get("next")
    remaining_urls = []
    while next_url and len(remaining_urls) < 4:
        remaining_urls.append(next_url)
        # Derive next page URL by incrementing the page parameter
        m = re.search(r'page=(\d+)', next_url)
        if m:
            next_page = int(m.group(1)) + 1
            next_url = re.sub(r'page=\d+', f'page={next_page}', next_url)
        else:
            break

    # Fetch remaining pages in parallel
    if remaining_urls:
        with ThreadPoolExecutor(max_workers=min(4, len(remaining_urls))) as pool:
            futures = {pool.submit(_fetch_phaohoa_json, u): u for u in remaining_urls}
            for future in as_completed(futures):
                try:
                    page_data = future.result()
                    page_results = page_data.get("results", [])
                    if isinstance(page_results, list):
                        results.extend(m for m in page_results if isinstance(m, dict))
                except Exception:
                    pass

    unique = {}
    for match in results:
        if not _phaohoa_is_active(match):
            continue
        key = match.get("id") or match.get("slug")
        if key:
            unique[str(key)] = match
    return list(unique.values())

def _phaohoa_has_stream(match: dict) -> bool:
    """Keep a non-terminal match when any usable stream is still published."""
    commentators = match.get("commentators") or []
    if any(
        str(commentator.get("stream_url") or commentator.get("streamUrl") or "").strip()
        for commentator in commentators
        if isinstance(commentator, dict)
    ):
        return True
    return any(
        str(match.get(field) or "").strip()
        for field in ("primary_stream_url", "backup_stream_url")
    )


def _phaohoa_is_active(match: dict) -> bool:
    status = str(match.get("status") or "").lower().strip()
    if status in FINISHED_STATUS_STRINGS:
        return False

    start_str = match.get("start_time", "")
    has_stream = _phaohoa_has_stream(match)
    if status not in ("scheduled", "upcoming", "") and start_str and not has_stream:
        try:
            dt = datetime.fromisoformat(start_str)
            if time.time() - dt.timestamp() > MATCH_MAX_AGE_SECONDS:
                return False
        except Exception:
            pass
    return True

def _pick_phaohoa_stream(match: dict) -> tuple:
    for c in (match.get("commentators") or []):
        url = (c.get("stream_url") or c.get("streamUrl") or "").strip()
        name = (c.get("nickname") or c.get("name") or "").strip()
        if url:
            return url, name
    primary = (match.get("primary_stream_url") or "").strip()
    if primary:
        return primary, ""
    backup = (match.get("backup_stream_url") or "").strip()
    if backup:
        return backup, ""
    return "", ""

def _phaohoa_logo(match: dict) -> str:
    parts = " ".join([
        match.get("sport_name", ""),
        match.get("sport_slug", ""),
        match.get("tournament_name", ""),
    ])
    return _logo_from_text(parts)

def _get_server_base_url() -> str:
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if render_url:
        return render_url.rstrip("/")
    domains = os.environ.get("REPLIT_DOMAINS", "")
    if domains:
        return f"https://{domains.split(',')[0].strip()}"
    app_url = os.environ.get("APP_URL", "")
    if app_url:
        return app_url.rstrip("/")
    return f"http://localhost:{os.environ.get('PORT', 5000)}"

def _build_phaohoa_lines(matches: list) -> list:
    lines = []
    try:
        matches = sorted(matches, key=lambda m: m.get("start_time") or "")
    except Exception:
        pass
    for match in matches:
        if not _phaohoa_is_active(match):
            continue
        slug = (match.get("slug") or "").strip()
        if not slug:
            continue
        stream_url, commentator = _pick_phaohoa_stream(match)
        if not stream_url:
            continue
        home       = (match.get("home_team_name") or "Home").strip()
        away       = (match.get("away_team_name") or "Away").strip()
        tournament = (match.get("tournament_name") or "").strip()
        logo       = _phaohoa_logo(match)
        start_str  = match.get("start_time", "")
        status     = str(match.get("status") or "").lower().strip()
        try:
            dt       = datetime.fromisoformat(start_str)
            dt_vn    = dt.astimezone(VN_TZ)
            time_str = dt_vn.strftime("%H:%M")
            date_str = dt_vn.strftime("%d/%m")
        except Exception:
            time_str = "--:--"
            date_str = "--/--"
        status_label = " LIVE" if status == "live" else ""
        if commentator:
            display = f"{time_str} - {date_str} | {home} VS {away} ({tournament}) | {commentator}{status_label}"
        else:
            display = f"{time_str} - {date_str} | {home} VS {away} ({tournament}){status_label}"
        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="Pháo Hoa TV",{display}')
        if "|" not in stream_url:
            stream_url += f"|Referer={PHAOHOA_FRONTEND_URL.rstrip('/')}/&User-Agent=Mozilla/5.0"
        lines.append(stream_url)
    return lines

# ══════════════════════════════════════════════════════════════════════════════
#  Giờ Vàng TV — JSON schedule + per-fixture stream details
# ══════════════════════════════════════════════════════════════════════════════

_GIOVANG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": f"{GIOVANG_FRONTEND_URL.rstrip('/')}/",
    "Accept": "application/json",
}
GIOVANG_FINISHED_STATUS_CODES = {"FT", "FINISHED", "END", "ENDED", "COMPLETE", "COMPLETED"}


def _discover_giovang_api_host() -> str:
    try:
        resp = _http_session.get(
            GIOVANG_FRONTEND_URL,
            timeout=10,
        )
        resp.raise_for_status()
        match = re.search(r'"liveJsonHost"\s*:\s*"([^"]+)"', resp.text)
        if match:
            host = match.group(1).strip().rstrip("/")
            return host if host.startswith(("http://", "https://")) else f"https://{host}"
    except Exception:
        pass
    return GIOVANG_API_HOST


def _get_giovang_api_host() -> str:
    now = time.time()
    if now - _giovang_api_cache["discovered_at"] > API_DISCOVERY_TTL:
        _giovang_api_cache["host"] = _discover_giovang_api_host()
        _giovang_api_cache["discovered_at"] = now
    return _giovang_api_cache["host"].rstrip("/")


def _fetch_giovang_json(url: str) -> dict:
    resp = _http_session.get(url, headers=_GIOVANG_HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("Giờ Vàng API returned a non-object response")
    return data


def _giovang_is_active(match: dict) -> bool:
    status_code = str(match.get("status_code") or "").upper().strip()
    status = str(match.get("status") or "").lower().strip()
    if status_code in GIOVANG_FINISHED_STATUS_CODES or status in FINISHED_STATUS_STRINGS:
        return False

    start_time = match.get("time_start")
    is_live = bool(match.get("is_live") or status_code in {"LIVE", "1H", "2H", "HT", "PEN", "ET"})
    if start_time and not is_live:
        try:
            if time.time() - float(start_time) > MATCH_MAX_AGE_SECONDS:
                return False
        except (TypeError, ValueError):
            pass
    return True


def _fetch_giovang_matches() -> list:
    host = _get_giovang_api_host()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            all_future = pool.submit(
                _fetch_giovang_json, f"{host}/storage/livestream/all.json"
            )
            live_future = pool.submit(
                _fetch_giovang_json, f"{host}/storage/livestream/live.json"
            )
            all_data = all_future.result()
            live_data = live_future.result()
    except Exception:
        _giovang_api_cache["discovered_at"] = 0
        raise

    unique = {}
    for match in (all_data.get("response", []) or []) + (live_data.get("response", []) or []):
        if not isinstance(match, dict) or not _giovang_is_active(match):
            continue
        key = match.get("id") or match.get("fi")
        if key:
            unique[str(key)] = match

    candidates = list(unique.values())
    if not candidates:
        return []

    # Keep list responses that already contain streams; only enrich missing ones.
    enriched = []
    needs_detail = []
    for match in candidates:
        if _pick_giovang_streams(match):
            enriched.append(match)
        else:
            needs_detail.append(match)

    def fetch_detail(match: dict) -> dict:
        fixture_id = match.get("id") or match.get("fi")
        detail_data = _fetch_giovang_json(f"{host}/api/fixtures/{quote(str(fixture_id), safe='')}")
        detail = detail_data.get("response")
        return detail if isinstance(detail, dict) else match

    errors = 0
    if needs_detail:
        with ThreadPoolExecutor(max_workers=min(8, len(needs_detail))) as pool:
            futures = {pool.submit(fetch_detail, match): match for match in needs_detail}
            for future in as_completed(futures):
                try:
                    detail = future.result()
                except Exception:
                    errors += 1
                    continue
                if _giovang_is_active(detail):
                    enriched.append(detail)

    if errors and not enriched:
        raise RuntimeError(f"Giờ Vàng fixture details failed for {errors} fixtures")
    return enriched

def _giovang_logo(match: dict) -> str:
    league = match.get("league") or {}
    parts = " ".join([
        str(match.get("type") or ""),
        str(league.get("title") or ""),
        str(league.get("code") or ""),
    ])
    return _logo_from_text(parts)


def _pick_giovang_streams(match: dict) -> list:
    streams = []
    seen_urls = set()
    commentators = match.get("blv") or []
    if isinstance(commentators, dict):
        commentators = list(commentators.values())
    for commentator in commentators:
        if not isinstance(commentator, dict):
            continue
        stream_url = next(
            (
                str(commentator.get(key) or "").strip()
                for key in (
                    "mobile_stream_url",
                    "pc_stream_url",
                    "link_stream_hd",
                    "link_stream_sd",
                )
                if str(commentator.get(key) or "").strip()
            ),
            "",
        )
        if not stream_url or stream_url in seen_urls:
            continue
        seen_urls.add(stream_url)
        name = str(
            commentator.get("blv_name")
            or commentator.get("name")
            or commentator.get("blv_key")
            or ""
        ).strip()
        streams.append((stream_url, name))
    return streams


def _build_giovang_lines(matches: list) -> list:
    try:
        matches = sorted(matches, key=lambda m: m.get("time_start") or 0)
    except Exception:
        pass

    lines = []
    for match in matches:
        if not _giovang_is_active(match):
            continue
        streams = _pick_giovang_streams(match)
        if not streams:
            continue

        teams = match.get("teams") or {}
        home = str((teams.get("home") or {}).get("name") or "Home").strip()
        away = str((teams.get("away") or {}).get("name") or "Away").strip()
        league = str((match.get("league") or {}).get("title") or "").strip()
        status_code = str(match.get("status_code") or "").upper().strip()
        try:
            dt_vn = datetime.fromtimestamp(float(match.get("time_start")), tz=VN_TZ)
            time_str = dt_vn.strftime("%H:%M")
            date_str = dt_vn.strftime("%d/%m")
        except (TypeError, ValueError, OSError, OverflowError):
            time_str = str(match.get("time") or "--:--")
            date_str = str(match.get("day_month") or "--/--")

        status_label = " LIVE" if status_code in {"LIVE", "1H", "2H", "HT", "PEN", "ET"} else ""
        logo = _giovang_logo(match)
        for stream_url, commentator in streams:
            display = f"{time_str} - {date_str} | {home} VS {away} ({league})"
            if commentator:
                display += f" | {commentator}"
            display += status_label
            lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="Giờ Vàng TV",{display}')
            lines.append(stream_url)
    return lines

# ══════════════════════════════════════════════════════════════════════════════
#  PhaLang TV — POST API at api.plapi202624081158.com/matches/graph
# ══════════════════════════════════════════════════════════════════════════════

PHALANG_LIVE_FRONTEND = os.environ.get("PHALANG_LIVE_FRONTEND", "https://phalang.live")

_PHALANG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Referer": f"{PHALANG_LIVE_FRONTEND.rstrip('/')}/",
    "Origin": PHALANG_LIVE_FRONTEND.rstrip("/"),
}


def _get_phalang_api_url() -> str:
    return PHALANG_API_URL.rstrip("/")


def _fetch_phalang_matches() -> list:
    api = _get_phalang_api_url()
    url = f"{api}/matches/graph"

    def post_query(payload: dict) -> list:
        resp = _http_session.post(url, json=payload, headers=_PHALANG_HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError("PhaLang API returned a non-object response")
        results = data.get("data", [])
        if not isinstance(results, list):
            raise RuntimeError("PhaLang API returned an invalid data list")
        return [m for m in results if isinstance(m, dict)]

    # One OR query replaces the former live + hot/top requests.
    matches = post_query({
        "limit": 100,
        "page": 1,
        "order_asc": "start_date",
        "queries": [
            {"field": "is_live", "type": "equal", "value": True},
            {"field": "is_hot", "type": "equal", "value": True},
            {"field": "is_top", "type": "equal", "value": True},
        ],
        "query_or": True,
    })

    unique = {}
    for match in matches:
        key = match.get("id")
        if key:
            unique[str(key)] = match
    return list(unique.values())

def _phalang_is_active(match: dict) -> bool:
    blv = (match.get("blv") or "").strip()
    if not blv:
        return False
    start_str = match.get("start_date", "")
    is_live = bool(match.get("is_live"))
    if start_str and not is_live:
        try:
            dt = datetime.fromisoformat(start_str)
            elapsed = time.time() - dt.timestamp()
            if elapsed > MATCH_MAX_AGE_SECONDS:
                return False
        except Exception:
            pass
    return True


def _fetch_phalang_stream(match_id: str) -> str:
    api = _get_phalang_api_url()
    try:
        resp = _http_session.get(f"{api}/match/{match_id}/live",
                            headers=_PHALANG_HEADERS, timeout=10)
        if resp.status_code != 200:
            return ""
        data = resp.json()
        for key in ("hd_1", "hd_2", "source"):
            url = (data.get(key) or "").strip()
            if url:
                return url
    except Exception:
        pass
    return ""


def _phalang_logo(match: dict) -> str:
    parts = " ".join([
        str(match.get("desc") or ""),
        str(match.get("league") or ""),
    ])
    return _logo_from_text(parts)


def _build_phalang_lines(matches: list) -> list:
    try:
        matches = sorted(matches, key=lambda m: m.get("start_date") or "")
    except Exception:
        pass

    active = [m for m in matches if _phalang_is_active(m)]
    stream_map = {}
    if active:
        with ThreadPoolExecutor(max_workers=min(8, len(active))) as pool:
            futures = {pool.submit(_fetch_phalang_stream, m.get("id", "")): m for m in active}
            for future in as_completed(futures):
                match = futures[future]
                try:
                    stream_map[match.get("id", "")] = future.result()
                except Exception:
                    stream_map[match.get("id", "")] = ""

    lines = []
    for match in active:
        mid = match.get("id", "")
        is_live = bool(match.get("is_live"))
        stream_url = stream_map.get(mid, "")

        home        = (match.get("team_1") or "Home").strip()
        away        = (match.get("team_2") or "Away").strip()
        league      = (match.get("league") or "").strip()
        commentator = (match.get("blv") or "").strip()
        logo        = _phalang_logo(match)
        start_str   = match.get("start_date", "")

        try:
            dt = datetime.fromisoformat(start_str)
            dt_vn = dt.astimezone(VN_TZ)
            time_str = dt_vn.strftime("%H:%M")
            date_str = dt_vn.strftime("%d/%m")
        except Exception:
            time_str = "--:--"
            date_str = "--/--"

        status_label = " LIVE" if is_live else ""
        display = f"{time_str} - {date_str} | {home} VS {away} ({league})"
        if commentator:
            display += f" | {commentator}"
        display += status_label
        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="PhaLang TV",{display}')
        if stream_url:
            if "|" not in stream_url:
                stream_url += f"|Referer={PHALANG_LIVE_FRONTEND.rstrip('/')}/&User-Agent=Mozilla/5.0"
            lines.append(stream_url)
        else:
            lines.append(f"{_get_server_base_url()}/upcoming/{mid}")
    return lines

# ══════════════════════════════════════════════════════════════════════════════
#  Dekiki (GitHub-hosted static list)
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_dekiki_lines() -> list:
    """Download the GitHub-hosted M3U, strip its header, return raw lines."""
    resp = _http_session.get(DEKIKI_M3U_URL, timeout=20)
    resp.raise_for_status()
    lines = []
    for line in resp.text.splitlines():
        stripped = line.rstrip()
        if not stripped or stripped.startswith("#EXTM3U"):
            continue
        lines.append(stripped)
    return lines

def _refresh_source_playlist(key: str, skip_recent_seconds: int = 15) -> list:
    """Refresh one M3U source without waiting for unrelated event providers."""
    fetchers = {
        "cola": lambda: _build_colatv_lines(_fetch_colatv_matches()),
        "phaohoa": lambda: _build_phaohoa_lines(_fetch_phaohoa_matches()),
        "giovang": lambda: _build_giovang_lines(_fetch_giovang_matches()),
        "phalang": lambda: _build_phalang_lines(_fetch_phalang_matches()),
        "dekiki": _fetch_dekiki_lines,
    }
    fetcher = fetchers.get(key)
    lock = _source_refresh_locks.get(key)
    if fetcher is None or lock is None:
        raise KeyError(f"Unknown playlist source: {key}")

    with lock:
        cached = _get_entry(key)
        if cached["content"] is not None and time.time() - cached["built_at"] < skip_recent_seconds:
            return cached["content"].decode("utf-8", errors="replace").splitlines()[1:]

        started = time.perf_counter()
        try:
            lines = fetcher()
            if not isinstance(lines, list):
                raise RuntimeError(f"{key} fetch returned a non-list")
            epg_header = f'#EXTM3U url-tvg="{EPG_URL}" x-tvg-url="{EPG_URL}"'
            _store(key, epg_header + "\n" + "\n".join(lines))
            _last_counts[key] = sum(1 for line in lines if line.startswith("#EXTINF"))
            with _source_timing_lock:
                _source_refresh_errors[key] = ""
            return lines
        except Exception as exc:
            with _source_timing_lock:
                _source_refresh_errors[key] = f"{type(exc).__name__}: {exc}"[:300]
            raise
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            with _source_timing_lock:
                _source_refresh_ms[key] = elapsed_ms

# ══════════════════════════════════════════════════════════════════════════════
#  Cache helpers — build compressed + ETag
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
#  Background pre-fetch (parallel sources)
# ══════════════════════════════════════════════════════════════════════════════

def _refresh_all_playlists():
    _resolve_all_frontends()
    errors = []

    def fetch_cola():
        return _refresh_source_playlist("cola")

    def fetch_phaohoa():
        return _refresh_source_playlist("phaohoa")

    def fetch_giovang():
        return _refresh_source_playlist("giovang")

    def fetch_phalang():
        return _refresh_source_playlist("phalang")

    def fetch_dekiki():
        return _refresh_source_playlist("dekiki")

    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {
            ex.submit(fetch_cola):     "cola",
            ex.submit(fetch_phaohoa):  "phaohoa",
            ex.submit(fetch_giovang):  "giovang",
            ex.submit(fetch_phalang):  "phalang",
            ex.submit(fetch_dekiki):   "dekiki",
        }
        results = {}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                results[key] = fut.result()
            except Exception as e:
                results[key] = []
                errors.append(f"{key}: {e}")

    cola_lines      = results.get("cola",      [])
    phaohoa_lines   = results.get("phaohoa",   [])
    giovang_lines   = results.get("giovang",   [])
    phalang_lines   = results.get("phalang",   [])
    dekiki_lines    = results.get("dekiki",    [])

    if not cola_lines and any(error.startswith("cola:") for error in errors):
        previous = _get_entry("cola")
        if previous.get("content"):
            previous_lines = previous["content"].decode("utf-8", errors="replace").splitlines()
            cola_lines = [line for line in previous_lines if not line.startswith("#EXTM3U")]
            errors.append("cola: kept last successful playlist")

    if not phaohoa_lines and any(error.startswith("phaohoa:") for error in errors):
        previous = _get_entry("phaohoa")
        if previous.get("content"):
            previous_lines = previous["content"].decode("utf-8", errors="replace").splitlines()
            phaohoa_lines = [line for line in previous_lines if not line.startswith("#EXTM3U")]
            errors.append("phaohoa: kept last successful playlist")

    if not giovang_lines and any(error.startswith("giovang:") for error in errors):
        previous = _get_entry("giovang")
        if previous.get("content"):
            previous_lines = previous["content"].decode("utf-8", errors="replace").splitlines()
            giovang_lines = [line for line in previous_lines if not line.startswith("#EXTM3U")]
            errors.append("giovang: kept last successful playlist")

    if not phalang_lines and any(error.startswith("phalang:") for error in errors):
        previous = _get_entry("phalang")
        if previous.get("content"):
            previous_lines = previous["content"].decode("utf-8", errors="replace").splitlines()
            phalang_lines = [line for line in previous_lines if not line.startswith("#EXTM3U")]
            errors.append("phalang: kept last successful playlist")

    err_str = "; ".join(errors)

    def count(lines):
        return sum(1 for l in lines if l.startswith("#EXTINF"))

    epg_header = f'#EXTM3U url-tvg="{EPG_URL}" x-tvg-url="{EPG_URL}"'

    _store("cola",      epg_header + "\n" + "\n".join(cola_lines))
    _store("phaohoa",   epg_header + "\n" + "\n".join(phaohoa_lines))
    _store("giovang",   epg_header + "\n" + "\n".join(giovang_lines))
    _store("phalang",   epg_header + "\n" + "\n".join(phalang_lines))
    _store("dekiki",    epg_header + "\n" + "\n".join(dekiki_lines))

    all_lines = (
        phalang_lines
        + phaohoa_lines
        + giovang_lines
        + cola_lines
        + dekiki_lines
    )
    combined_text = epg_header + "\n" + "\n".join(all_lines)
    if err_str:
        combined_text += f"\n# Errors: {err_str}"
    _store("combined", combined_text)

    _last_counts.update({
        "cola":         count(cola_lines),
        "phaohoa":      count(phaohoa_lines),
        "giovang":      count(giovang_lines),
        "phalang":      count(phalang_lines),
        "dekiki":       count(dekiki_lines),
        "refreshed_at": time.time(),
        "last_error":   err_str,
    })

def _refresh_all_with_lock(blocking: bool = True) -> bool:
    global _refresh_in_progress
    if not _refresh_lock.acquire(blocking=blocking):
        return False
    try:
        _refresh_in_progress = True
        _refresh_all_playlists()
        return True
    finally:
        _refresh_in_progress = False
        _refresh_lock.release()

def _prefetch_loop():
    time.sleep(3)
    while True:
        try:
            _refresh_all_with_lock(blocking=False)
        except Exception:
            pass
        time.sleep(PREFETCH_INTERVAL)

def _get_entry(key: str):
    entry = _playlist_cache[key]
    with entry["lock"]:
        return {
            "content": entry["content"],
            "gz": entry["gz"],
            "etag": entry["etag"],
            "built_at": entry["built_at"],
        }

# ══════════════════════════════════════════════════════════════════════════════
#  Flask routes
# ══════════════════════════════════════════════════════════════════════════════

def _m3u_response(key: str, filename: str) -> Response:
    _ensure_background_tasks()
    entry = _get_entry(key)
    refresh_error = ""
    cache_age = time.time() - entry["built_at"] if entry["content"] is not None else None
    needs_refresh = entry["content"] is None or cache_age >= PREFETCH_INTERVAL

    if needs_refresh:
        try:
            if key == "combined":
                _refresh_all_with_lock(blocking=True)
            else:
                _refresh_source_playlist(key)
        except Exception as e:
            # Keep the last successful body, but expose the refresh failure in headers.
            refresh_error = f"{type(e).__name__}: {e}"[:300]
        entry = _get_entry(key)
        if entry["content"] is None:
            status = 500 if key == "combined" else 502
            return Response(
                f"Upstream error for {key}: {refresh_error or 'playlist cache is not ready'}",
                status=status,
                mimetype="text/plain",
            )

    etag = entry["etag"]
    cache_control = _PLAYLIST_CACHE_CONTROL

    # Always send the current body. Some IPTV clients incorrectly reuse stale
    # local content after a 304 response, even when the server says no-cache.
    accept_enc = request.headers.get("Accept-Encoding", "")
    use_gzip   = "gzip" in accept_enc and entry["gz"] is not None
    body = entry["gz"] if use_gzip else entry["content"]

    resp = Response(body, mimetype="application/x-mpegurl")
    # Do not emit ETag: some Vercel/IPTV clients turn it into a stale 304 response.
    resp.headers["Cache-Control"]       = cache_control
    resp.headers["Pragma"]              = "no-cache"
    resp.headers["Expires"]             = "0"
    resp.headers["Surrogate-Control"]   = "no-store"
    resp.headers["CDN-Cache-Control"]   = "no-store"
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.headers["Vary"]                = "Accept-Encoding"
    resp.headers["X-Playlist-Built-At"]  = str(int(entry["built_at"]))
    resp.headers["X-Playlist-Cache"]     = "stale-fallback" if refresh_error else ("refreshed" if needs_refresh else "memory")
    if refresh_error:
        resp.headers["X-Playlist-Refresh-Error"] = refresh_error
    if use_gzip:
        resp.headers["Content-Encoding"] = "gzip"
    return resp

@app.route("/live.m3u")
def live_m3u():
    return _m3u_response("combined", "live.m3u")

@app.route("/cola.m3u")
def cola_m3u():
    return _m3u_response("cola", "cola.m3u")

@app.route("/phaohoa.m3u")
def phaohoa_m3u():
    return _m3u_response("phaohoa", "phaohoa.m3u")

@app.route("/giovang.m3u")
def giovang_m3u():
    return _m3u_response("giovang", "giovang.m3u")

@app.route("/phalang.m3u")
def phalang_m3u():
    return _m3u_response("phalang", "phalang.m3u")

@app.route("/dekiki.m3u")
def dekiki_m3u():
    return _m3u_response("dekiki", "dekiki.m3u")

@app.route("/status.json")
def status_json():
    from flask import jsonify
    ra    = _last_counts.get("refreshed_at", 0)
    ra_vn = datetime.fromtimestamp(ra, tz=VN_TZ).strftime("%H:%M:%S %d/%m/%Y") if ra else None
    next_s = max(int(PREFETCH_INTERVAL - (time.time() - ra)), 0) if ra else None
    with _source_timing_lock:
        source_refresh_ms = dict(_source_refresh_ms)
        source_refresh_errors = dict(_source_refresh_errors)
    return jsonify({
        "ok":           True,
        "refreshed_at": ra_vn,
        "next_refresh_in_seconds": next_s,
        "last_error":   _last_counts.get("last_error", ""),
        "source_refresh_ms": source_refresh_ms,
        "source_refresh_errors": source_refresh_errors,
        "channels": {
            "total":      sum(_last_counts.get(k, 0) for k in ("cola","phaohoa","giovang","phalang","dekiki")),
            "cola_tv":    _last_counts.get("cola",    0),
            "phaohoa_tv": _last_counts.get("phaohoa", 0),
            "giovang_tv": _last_counts.get("giovang", 0),
            "phalang_tv": _last_counts.get("phalang", 0),
            "dekiki_tv":  _last_counts.get("dekiki",  0),
        },
        "sources": {
            "cola_tv":    {"api": _colatv_api_cache.get("url"),  "status": "ok" if _last_counts.get("cola",0)    > 0 else "empty"},
            "phaohoa_tv": {"api": PHAOHOA_API_URL,               "status": "ok" if _last_counts.get("phaohoa",0) > 0 else "empty"},
            "giovang_tv": {"api": _giovang_api_cache.get("host"), "status": "ok" if _last_counts.get("giovang",0) > 0 else "empty"},
            "phalang_tv": {"api": PHALANG_API_URL,                "status": "ok" if _last_counts.get("phalang",0) > 0 else "empty"},
            "dekiki_tv":  {"api": "github-static",               "status": "ok" if _last_counts.get("dekiki",0)  > 0 else "empty"},
        },
    })

@app.route("/ping")
def ping():
    return Response("OK", mimetype="text/plain")

@app.route("/upcoming/<match_id>")
def upcoming(match_id: str):
    now = time.time()
    cached = None
    with _upcoming_cache_lock:
        cached = _upcoming_cache.get(match_id)
    if cached and now - cached["ts"] < _upcoming_cache_ttl:
        stream_url = cached["url"]
    else:
        stream_url = _fetch_phalang_stream(match_id)
        with _upcoming_cache_lock:
            # Evict oldest entries to prevent unbounded memory growth
            if len(_upcoming_cache) >= _UPCOMING_CACHE_MAX:
                oldest = min(_upcoming_cache, key=lambda k: _upcoming_cache[k]["ts"])
                del _upcoming_cache[oldest]
            _upcoming_cache[match_id] = {"url": stream_url, "ts": now}

    if not stream_url:
        return Response(
            "Tran chua bat dau / Match not started",
            mimetype="text/plain",
            status=503,
        )
    if "|" not in stream_url:
        stream_url += (
            f"|Referer={PHALANG_LIVE_FRONTEND.rstrip('/')}/"
            f"&User-Agent=Mozilla/5.0"
        )
    return redirect(stream_url, code=302)

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

    cola_count     = _last_counts.get("cola",      0)
    phaohoa_count  = _last_counts.get("phaohoa",   0)
    giovang_count  = _last_counts.get("giovang",   0)
    phalang_count  = _last_counts.get("phalang",   0)
    dekiki_count   = _last_counts.get("dekiki",    0)
    total          = cola_count + phaohoa_count + giovang_count + phalang_count + dekiki_count

    return (
        "<h2>🎬 IPTV M3U Server</h2>"
        "<h3>📋 Playlist</h3><ul>"
        "<li><a href='/live.m3u'>/live.m3u</a> — Tất cả nguồn gộp lại</li>"
        "<li><a href='/cola.m3u'>/cola.m3u</a> — Cola TV only</li>"
        "<li><a href='/phaohoa.m3u'>/phaohoa.m3u</a> — Pháo Hoa TV only</li>"
         "<li><a href='/giovang.m3u'>/giovang.m3u</a> — Giờ Vàng TV only</li>"
        "<li><a href='/phalang.m3u'>/phalang.m3u</a> — PhaLang TV only</li>"
        "<li><a href='/dekiki.m3u'>/dekiki.m3u</a> — Kênh TV Việt (dekiki)</li>"
        "</ul>"
        "<h3>📊 Trạng thái</h3>"
        f"<p>📺 Tổng kênh: <strong>{total}</strong>"
        f" &nbsp;(🏆 Live: {cola_count + phaohoa_count + giovang_count + phalang_count}"
        f" | 📡 TV: {dekiki_count})</p>"
        f"<p>🕐 Cập nhật lần cuối: <strong>{dt_str}</strong></p>"
        f"<p>⏳ Cập nhật tiếp theo: <strong>{next_str}</strong></p>"
        f"<p>🟢 Cola TV: <strong>{cola_count} kênh</strong>"
        f"&nbsp;|&nbsp; <code>{_colatv_api_cache['url']}</code></p>"
        f"<p>🟢 Pháo Hoa TV: <strong>{phaohoa_count} kênh</strong>"
        f"&nbsp;|&nbsp; <code>{PHAOHOA_API_URL}</code></p>"
        f"<p>🟢 Giờ Vàng TV: <strong>{giovang_count} kênh</strong>"
        f"&nbsp;|&nbsp; <code>{_giovang_api_cache['host']}</code></p>"
        f"<p>🟢 PhaLang TV: <strong>{phalang_count} kênh</strong>"
        f"&nbsp;|&nbsp; <code>{PHALANG_API_URL}</code></p>"
         f"<p>📡 Kênh TV (dekiki): <strong>{dekiki_count} kênh</strong></p>"
        f"<p>📻 EPG: <a href='{EPG_URL}' target='_blank'>{EPG_URL}</a></p>"
        f"{err_html}"
        "<h3>⚙️ Tối ưu băng thông</h3><ul>"
        "<li>Gzip nén tự động (giảm ~70% dữ liệu truyền)</li>"
        "<li>ETag + HTTP 304 — client có cache không cần tải lại</li>"
        "<li>Cache-Control: no-store, no-cache, must-revalidate, max-age=0</li>"
        "<li>1 worker process + 16 threads — cache dùng chung, không fetch trùng lặp</li>"
        "<li>Các nguồn fetch song song (ThreadPoolExecutor)</li>"
        f"<li>Làm mới cache mỗi <strong>{PREFETCH_INTERVAL // 60} phút</strong></li>"
        "</ul>"
        "<h3>⚡ Pháo Hoa TV — Direct Stream</h3><ul>"
         "<li>Link stream được fetch cùng playlist và cache như CoLa</li>"
         "<li>Chỉ hiển thị trận đã có stream URL</li>"
         "</ul>"
         "<h3>⚡ Giờ Vàng TV — Direct Stream</h3><ul>"
         "<li>Schedule và stream được lấy từ JSON public, chi tiết fixture fetch song song</li>"
         "<li>Chỉ hiển thị trận còn hiệu lực và có ít nhất một bình luận viên có stream</li>"
         "</ul>"
         "<h3>⚡ PhaLang TV — Direct Stream</h3><ul>"
         "<li>Stream được lấy từ API, chỉ 1 nguồn duy nhất có bình luận viên tiếng Việt</li>"
         "<li>Fetch song song live + hot matches, gộp và lọc trùng</li>"
         "</ul>"
    )

# ══════════════════════════════════════════════════════════════════════════════
#  Keep-alive self-ping
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
#  Startup
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    _ensure_background_tasks()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

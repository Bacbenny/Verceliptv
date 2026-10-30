import gzip
import html
import ipaddress
import json
import os
import re
import threading
import time
from io import BytesIO
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urlencode, urljoin, urlsplit

import cloudscraper
import certifi
import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps
from flask import Flask, Response, has_request_context, request, redirect
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)

_PLAYLIST_CACHE_CONTROL = "public, max-age=0, s-maxage=30, stale-while-revalidate=120, must-revalidate"
_PLAYLIST_EDGE_CONTROL = "public, s-maxage=30, stale-while-revalidate=120"
_STATUS_CACHE_CONTROL = "no-store, no-cache, max-age=0, private"


@app.after_request
def _disable_playlist_caching(response):
    """Prevent browsers, IPTV clients, and edge caches from reusing playlists."""
    if request.path.endswith(".m3u"):
        response.headers["Cache-Control"] = _PLAYLIST_CACHE_CONTROL
        response.headers["Surrogate-Control"] = _PLAYLIST_EDGE_CONTROL
        response.headers["CDN-Cache-Control"] = _PLAYLIST_EDGE_CONTROL
    elif request.path.endswith(".json"):
        response.headers["Cache-Control"] = _STATUS_CACHE_CONTROL
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["Surrogate-Control"] = "no-store"
        response.headers["CDN-Cache-Control"] = "no-store"
    return response

# ─── Shared HTTP sessions (connection reuse) ───────────────────────────────────
# Reusing TCP+TLS connections across calls to the same host saves
# 200-500ms per request under load.
_http_session = requests.Session()
_http_session.verify = os.environ.get("REQUESTS_CA_BUNDLE") or certifi.where()
_http_session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
_http_retry = Retry(
    total=2,
    connect=2,
    read=2,
    status=2,
    backoff_factor=0.25,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET", "HEAD", "OPTIONS", "POST", "PUT"}),
    raise_on_status=False,
)
_http_session.mount("https://", HTTPAdapter(max_retries=_http_retry))
_http_session.mount("http://", HTTPAdapter(max_retries=_http_retry))
_cloudflare_kv_session = requests.Session()
_cloudflare_kv_session.verify = _http_session.verify
_cloudflare_kv_session.headers.update(
    {"User-Agent": "Verceliptv-KV/1.0"}
)
_phalang_prewarm_session = requests.Session()
_phalang_prewarm_session.verify = _http_session.verify
_phalang_prewarm_session.headers.update(
    {"User-Agent": "Mozilla/5.0", "Accept": "application/vnd.apple.mpegurl"}
)

# ─── PhaLang TV config ───────────────────────────────────────────────────────
PHALANG_FRONTEND_URL = os.environ.get("PHALANG_FRONTEND", "https://phalang.tv")
PHALANG_API_URL      = os.environ.get("PHALANG_API", "https://api.plapi202624081158.com")

# ─── Giờ Vàng TV config ──────────────────────────────────────────────────────
GIOVANG_FRONTEND_URL = os.environ.get("GIOVANG_FRONTEND", "https://giovang.asia")
GIOVANG_API_HOST     = os.environ.get(
    "GIOVANG_API_HOST", "https://live-api.keonhacaitp.one"
)
# Bound slow upstream responses so one source cannot hold a cold playlist open.
GIOVANG_API_TIMEOUT = float(os.environ.get("GIOVANG_API_TIMEOUT", "10"))
GIOVANG_DETAIL_WINDOW_SECONDS = max(
    0, int(os.environ.get("GIOVANG_DETAIL_WINDOW_SECONDS", "21600"))
)  # fetch upcoming/live fixture details only inside this window
GIOVANG_DETAIL_CACHE_TTL = max(
    30, int(os.environ.get("GIOVANG_DETAIL_CACHE_TTL", "300"))
)  # refresh eligible details with the playlist cache

# ─── Persistent playlist cache (Cloudflare KV) ────────────────────────────────
# KV is optional: the app keeps working with the in-process cache when these
# variables are absent or Cloudflare is temporarily unavailable.
CLOUDFLARE_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_KV_NAMESPACE_ID = os.environ.get("CLOUDFLARE_KV_NAMESPACE_ID", "")
CLOUDFLARE_KV_PREFIX = os.environ.get("CLOUDFLARE_KV_PREFIX", "verceliptv:")
CLOUDFLARE_KV_TIMEOUT = float(os.environ.get("CLOUDFLARE_KV_TIMEOUT", "3"))
CLOUDFLARE_KV_MAX_AGE = int(os.environ.get("CLOUDFLARE_KV_MAX_AGE", "1800"))
CLOUDFLARE_KV_BASE_URL = (
    f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}"
    f"/storage/kv/namespaces/{CLOUDFLARE_KV_NAMESPACE_ID}/values"
)

# ─── Dekiki (GitHub-hosted static list) + EPG ────────────────────────────────
DEKIKI_M3U_URL = os.environ.get(
    "DEKIKI_M3U_URL",
    "https://raw.githubusercontent.com/Bacbenny/dekiki/refs/heads/main/film4k.m3u",
)
EPG_URL = os.environ.get("EPG_URL", "https://lichphatsong.io.vn/epg.xml")

# ─── Shared config ────────────────────────────────────────────────────────────
VN_TZ                = timezone(timedelta(hours=7))
SELF_PING_INTERVAL   = 240   # seconds
PREFETCH_INTERVAL    = 300   # seconds — refresh cache every 5 minutes
PHALANG_PLAYLIST_INTERVAL = max(
    30, int(os.environ.get("PHALANG_PLAYLIST_INTERVAL", "120"))
)  # seconds — discover new/upcoming matches
PHALANG_STREAM_CACHE_TTL = max(
    5, int(os.environ.get("PHALANG_STREAM_CACHE_TTL", "30"))
)  # seconds — successful resolver result
PHALANG_STREAM_MISS_TTL = max(
    1, int(os.environ.get("PHALANG_STREAM_MISS_TTL", "3"))
)  # seconds — not-live-yet resolver result
PHALANG_PREWARM_WINDOW = max(
    0, int(os.environ.get("PHALANG_PREWARM_WINDOW", "120"))
)  # seconds before kickoff
PHALANG_PREWARM_MAX_MATCHES = max(
    0, min(4, int(os.environ.get("PHALANG_PREWARM_MAX_MATCHES", "3")))
)  # bounded upstream fan-out
PHALANG_PREWARM_WAIT = max(
    0, min(3, int(os.environ.get("PHALANG_PREWARM_WAIT", "1")))
)  # seconds allowed during playlist refresh; unfinished matches use resolver fallback
PHALANG_STREAM_KV_TTL = max(
    60, min(600, int(os.environ.get("PHALANG_STREAM_KV_TTL", "180")))
)  # shared resolver URL cache; enough to bridge the kickoff window
PHALANG_STREAM_KV_TIMEOUT = max(
    0.2, min(2.0, float(os.environ.get("PHALANG_STREAM_KV_TIMEOUT", "0.7")))
)  # keep KV failures from delaying channel opens
PHALANG_HLS_PREWARM_TIMEOUT = max(
    1, min(8, int(os.environ.get("PHALANG_HLS_PREWARM_TIMEOUT", "5")))
)
PHALANG_HLS_ALLOWED_HOSTS = frozenset(
    {"pull.digitalcdn.net", "100ycdn.com"}
)
PHALANG_QUERY_PAGE_SIZE = 50
PHALANG_QUERY_MAX_PAGES = 5
API_DISCOVERY_TTL    = 3600  # seconds — re-discover API URL every 1 hour

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


def _safe_logo_source(value: object) -> str:
    """Accept public HTTP(S) image URLs only; these are embedded, never fetched here."""
    raw = str(value or "").strip()
    if not raw or len(raw) > 2048 or any(ord(char) < 32 for char in raw):
        return ""
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme.lower() not in {"http", "https"} or not host:
            return ""
        if parsed.username or parsed.password or host == "localhost" or host.endswith(
            (".localhost", ".local")
        ):
            return ""
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            pass
    except ValueError:
        return ""
    return raw


_LOGO_URL_KEYS = (
    "logo_url", "logoUrl", "logo", "image_url", "imageUrl", "image",
    "img_url", "imgUrl", "badge_url", "badgeUrl", "badge",
    "crest_url", "crestUrl", "crest", "emblem_url", "emblemUrl",
    "emblem", "icon_url", "iconUrl", "icon", "src", "url",
)


def _extract_logo_url(value: object) -> str:
    if isinstance(value, str):
        return _safe_logo_source(value)
    if isinstance(value, dict):
        for key in _LOGO_URL_KEYS:
            if key in value:
                found = _extract_logo_url(value[key])
                if found:
                    return found
    elif isinstance(value, (list, tuple)):
        for item in value[:5]:
            found = _extract_logo_url(item)
            if found:
                return found
    return ""


def _first_logo_url(*values: object) -> str:
    for value in values:
        found = _extract_logo_url(value)
        if found:
            return found
    return ""


def _fixture_name(value: object, fallback: str) -> str:
    if isinstance(value, str):
        return value.strip()[:80] or fallback
    if isinstance(value, dict):
        for key in ("name", "title", "short_name", "shortName", "display_name"):
            name = value.get(key)
            if isinstance(name, str) and name.strip():
                return name.strip()[:80]
    return fallback


def _logo_context_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _fixture_name(value, "")
    if isinstance(value, (list, tuple)):
        return " ".join(filter(None, (_logo_context_text(item) for item in value)))
    return ""


def _match_logo_parts(match: dict, provider: str) -> tuple[str, str, str, str, str, str]:
    if provider == "giovang":
        teams = match.get("teams") if isinstance(match.get("teams"), dict) else {}
        home = (
            teams.get("home") or match.get("team_1") or match.get("player_1")
            or match.get("home_team") or match.get("home") or {}
        )
        away = (
            teams.get("away") or match.get("team_2") or match.get("player_2")
            or match.get("away_team") or match.get("away") or {}
        )
        league = (
            match.get("league") or match.get("competition")
            or match.get("tournament") or match.get("category") or {}
        )
        home_name = _fixture_name(home, "Home")
        away_name = _fixture_name(away, "Away")
        league_name = _fixture_name(league, "")
        home_logo = _first_logo_url(
            match.get("home_team_logo"), match.get("home_team_logo_url"),
            match.get("home_logo"), match.get("team_1_logo"),
            match.get("team_1_logo_url"), match.get("player_1_logo"),
            match.get("home_player_logo"), match.get("participant_1_logo"), home,
        )
        away_logo = _first_logo_url(
            match.get("away_team_logo"), match.get("away_team_logo_url"),
            match.get("away_logo"), match.get("team_2_logo"),
            match.get("team_2_logo_url"), match.get("player_2_logo"),
            match.get("away_player_logo"), match.get("participant_2_logo"), away,
        )
        league_logo = _first_logo_url(
            match.get("league_logo"), match.get("league_logo_url"),
            match.get("competition_logo"), match.get("tournament_logo"),
            match.get("category_logo"), league,
        )
    else:
        home = (
            match.get("team_1") or match.get("player_1") or match.get("home")
            or match.get("home_team") or match.get("participant_1") or {}
        )
        away = (
            match.get("team_2") or match.get("player_2") or match.get("away")
            or match.get("away_team") or match.get("participant_2") or {}
        )
        league = (
            match.get("league") or match.get("tournament")
            or match.get("competition") or match.get("category") or {}
        )
        home_name = _fixture_name(home, "Home")
        away_name = _fixture_name(away, "Away")
        league_name = _fixture_name(league, "")
        home_logo = _first_logo_url(
            match.get("team_1_logo"), match.get("team_1_logo_url"),
            match.get("home_team_logo"), match.get("home_team_logo_url"),
            match.get("home_logo"), match.get("player_1_logo"),
            match.get("participant_1_logo"), home,
        )
        away_logo = _first_logo_url(
            match.get("team_2_logo"), match.get("team_2_logo_url"),
            match.get("away_team_logo"), match.get("away_team_logo_url"),
            match.get("away_logo"), match.get("player_2_logo"),
            match.get("participant_2_logo"), away,
        )
        league_logo = _first_logo_url(
            match.get("league_logo"), match.get("league_logo_url"),
            match.get("tournament_logo"), match.get("competition_logo"),
            match.get("category_logo"), league,
        )
    return home_logo, league_logo, away_logo, home_name, league_name, away_name


MATCH_LOGO_RENDER_VERSION = "8"


def _composite_match_logo_url(match: dict, provider: str, parts: str) -> str:
    home_logo, league_logo, away_logo, home_name, league_name, away_name = (
        _match_logo_parts(match, provider)
    )
    # Give every sport a relevant centre mark when the provider has no league
    # logo. Team/player logos still take priority on either side.
    league_logo = league_logo or _logo_from_text(parts)
    query = urlencode({
        "home": home_logo, "league": league_logo, "away": away_logo,
        "home_name": home_name, "league_name": league_name, "away_name": away_name,
        "v": MATCH_LOGO_RENDER_VERSION,
    })
    return f"{_get_server_base_url()}/match-logo.png?{query}"


def _match_or_sport_logo(match: dict, provider: str, parts: str) -> str:
    return _composite_match_logo_url(match, provider, parts)


# ─── API URL caches ───────────────────────────────────────────────────────────
# The configured API host is known and should be used immediately.
# Discovery is retried only after an API failure, avoiding a frontend probe on cold start.
_giovang_api_cache  = {"host": GIOVANG_API_HOST, "discovered_at": time.time()}
_phalang_api_cache  = {"url": PHALANG_API_URL,   "discovered_at": 0}

# ─── Auto domain resolution ───────────────────────────────────────────────────
# ─── Playlist content cache ───────────────────────────────────────────────────
def _empty_entry():
    return {"content": None, "gz": None, "built_at": 0,
            "lock": threading.Lock()}

_playlist_cache = {
    "combined": _empty_entry(),
    "giovang":  _empty_entry(),
    "phalang":  _empty_entry(),
    "dekiki":   _empty_entry(),
}

_last_counts = {
    "refreshed_at": 0, "last_error": "",
}

_background_lock = threading.Lock()
_background_started = False

_refresh_lock = threading.Lock()
_refresh_in_progress = False

_stale_refresh_lock = threading.Lock()
_stale_refresh_keys = set()

_source_refresh_locks = {
    key: threading.Lock()
    for key in ("giovang", "phalang", "dekiki")
}
_source_timing_lock = threading.Lock()
_source_refresh_ms = {}
_source_refresh_errors = {}

_giovang_detail_cache = {}
_giovang_detail_cache_lock = threading.Lock()
_giovang_detail_stats = {
    "last_requested": 0,
    "last_successful": 0,
    "last_errors": 0,
    "last_cache_hits": 0,
    "last_reused_outside_window": 0,
    "last_skipped_without_cache": 0,
}

_kv_hydrated_keys = set()
_kv_hydration_locks = {
    key: threading.Lock() for key in _playlist_cache
}
_cloudflare_kv_last_error = ""
_cloudflare_kv_last_write_at = 0
_cloudflare_kv_backoff_until = 0
_removed_group_cache_purge_attempted = False
_removed_group_cache_purge_lock = threading.Lock()


def _cloudflare_api_token() -> str:
    """Normalize a token copied from Vercel without ever logging its value."""
    token = str(CLOUDFLARE_API_TOKEN or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _cloudflare_kv_enabled() -> bool:
    return bool(
        _cloudflare_api_token()
        and CLOUDFLARE_ACCOUNT_ID
        and CLOUDFLARE_KV_NAMESPACE_ID
    )


def _cloudflare_kv_url(key: str) -> str:
    remote_key = quote(f"{CLOUDFLARE_KV_PREFIX}{key}", safe="")
    return f"{CLOUDFLARE_KV_BASE_URL}/{remote_key}"


def _cloudflare_kv_headers() -> dict:
    return {
        "Authorization": f"Bearer {_cloudflare_api_token()}",
        "Content-Type": "application/json",
    }


def _record_cloudflare_kv_error(exc) -> None:
    global _cloudflare_kv_last_error, _cloudflare_kv_backoff_until
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    reason = getattr(response, "reason", "") or ""
    if status:
        message = f"{type(exc).__name__}: HTTP {status} {reason}"[:160]
    else:
        message = type(exc).__name__

    with _source_timing_lock:
        _cloudflare_kv_last_error = message
        if status == 429:
            retry_after = None
            try:
                retry_after = float(response.headers.get("Retry-After", ""))
            except (TypeError, ValueError):
                pass
            cooldown = min(300, max(30, retry_after or 60))
            _cloudflare_kv_backoff_until = time.time() + cooldown


def _cloudflare_kv_in_backoff() -> bool:
    with _source_timing_lock:
        return time.time() < _cloudflare_kv_backoff_until


def _clear_cloudflare_kv_error() -> None:
    global _cloudflare_kv_last_error, _cloudflare_kv_backoff_until
    with _source_timing_lock:
        _cloudflare_kv_last_error = ""
        _cloudflare_kv_backoff_until = 0


def _cloudflare_kv_get(key: str, timeout: float | None = None):
    if not _cloudflare_kv_enabled() or _cloudflare_kv_in_backoff():
        return None
    try:
        response = _cloudflare_kv_session.get(
            _cloudflare_kv_url(key),
            headers=_cloudflare_kv_headers(),
            timeout=timeout if timeout is not None else CLOUDFLARE_KV_TIMEOUT,
        )
        if response.status_code == 404:
            _clear_cloudflare_kv_error()
            return None
        response.raise_for_status()
        _clear_cloudflare_kv_error()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return None
        content = payload.get("content")
        built_at = float(payload.get("built_at") or 0)
        if not isinstance(content, str) or built_at <= 0:
            return None
        if time.time() - built_at > CLOUDFLARE_KV_MAX_AGE:
            return None
        return content, built_at
    except Exception as exc:
        _record_cloudflare_kv_error(exc)
        return None


def _cloudflare_kv_put(
    key: str,
    text: str,
    built_at: float,
    expiration_ttl: int | None = None,
    timeout: float | None = None,
) -> None:
    global _cloudflare_kv_last_write_at, _cloudflare_kv_last_error
    global _cloudflare_kv_backoff_until
    if not _cloudflare_kv_enabled() or _cloudflare_kv_in_backoff():
        return
    payload = json.dumps(
        {"version": 1, "built_at": built_at, "content": text},
        ensure_ascii=False,
    )
    try:
        params = (
            {"expiration_ttl": max(60, int(expiration_ttl))}
            if expiration_ttl is not None
            else None
        )
        headers = _cloudflare_kv_headers()
        headers["Content-Type"] = "application/octet-stream"
        response = _cloudflare_kv_session.put(
            _cloudflare_kv_url(key),
            params=params,
            headers=headers,
            data=payload.encode("utf-8"),
            timeout=timeout if timeout is not None else CLOUDFLARE_KV_TIMEOUT,
        )
        response.raise_for_status()
        with _source_timing_lock:
            _cloudflare_kv_last_write_at = time.time()
            _cloudflare_kv_last_error = ""
            _cloudflare_kv_backoff_until = 0
    except Exception as exc:
        _record_cloudflare_kv_error(exc)


def _cloudflare_kv_delete(key: str) -> None:
    if not _cloudflare_kv_enabled() or _cloudflare_kv_in_backoff():
        return
    try:
        response = _cloudflare_kv_session.delete(
            _cloudflare_kv_url(key),
            headers=_cloudflare_kv_headers(),
            timeout=CLOUDFLARE_KV_TIMEOUT,
        )
        if response.status_code != 404:
            response.raise_for_status()
        _clear_cloudflare_kv_error()
    except Exception as exc:
        _record_cloudflare_kv_error(exc)


def _purge_removed_group_cache() -> None:
    """Delete the obsolete source-specific cache once; combined is scrubbed on hydrate."""
    global _removed_group_cache_purge_attempted
    with _removed_group_cache_purge_lock:
        if _removed_group_cache_purge_attempted:
            return
        _removed_group_cache_purge_attempted = True
        try:
            _cloudflare_kv_delete("phaohoa")
        except Exception as exc:
            _record_cloudflare_kv_error(exc)


def _persist_cloudflare_kv(key: str, text: str, built_at: float) -> None:
    # The combined playlist is written synchronously so a serverless invocation
    # cannot finish before the durable fallback exists. Individual playlists
    # are written in daemon threads to keep source refreshes fast.
    if key == "combined":
        _cloudflare_kv_put(key, text, built_at)
    else:
        threading.Thread(
            target=_cloudflare_kv_put,
            args=(key, text, built_at),
            daemon=True,
            name=f"kv-write-{key}",
        ).start()


def _hydrate_from_cloudflare_kv(key: str) -> bool:
    if not _cloudflare_kv_enabled() or key in _kv_hydrated_keys:
        return False
    lock = _kv_hydration_locks[key]
    with lock:
        if key in _kv_hydrated_keys:
            return False
        hydrated = False
        try:
            saved = _cloudflare_kv_get(key)
            if not saved:
                return False
            text, built_at = saved
            cleaned_text = _repair_match_logo_urls(text)
            if key == "combined":
                cleaned_text = _strip_removed_groups(cleaned_text)
            if cleaned_text != text:
                text = cleaned_text
                _cloudflare_kv_put(key, text, built_at)
            _store_local(key, text, built_at)
            _last_counts[key] = sum(
                1 for line in text.splitlines() if line.startswith("#EXTINF")
            )
            hydrated = True
            return True
        finally:
            if hydrated:
                _kv_hydrated_keys.add(key)


_upcoming_cache: dict[str, dict] = {}
_upcoming_cache_lock = threading.Lock()
_upcoming_inflight: dict[str, threading.Event] = {}
_upcoming_cache_ttl = PHALANG_STREAM_CACHE_TTL
_upcoming_miss_ttl = PHALANG_STREAM_MISS_TTL
_UPCOMING_CACHE_MAX = 200
_phalang_prewarm_pool = ThreadPoolExecutor(
    max_workers=PHALANG_PREWARM_MAX_MATCHES or 1,
    thread_name_prefix="phalang-prewarm",
)
_phalang_resolver_stats = {
    "persistent_cache_hits": 0,
    "persistent_cache_misses": 0,
    "manifest_prewarm_attempts": 0,
    "manifest_prewarm_successes": 0,
    "manifest_prewarm_errors": 0,
    "manifest_prewarm_last_ms": 0,
    "manifest_prewarm_last_at": 0,
}


def _increment_phalang_resolver_stat(name: str, amount: int = 1) -> None:
    with _source_timing_lock:
        _phalang_resolver_stats[name] = (
            int(_phalang_resolver_stats.get(name, 0)) + amount
        )


def _valid_phalang_stream_url(stream_url: str) -> bool:
    if not stream_url or len(stream_url) > 4096:
        return False
    try:
        parsed = urlsplit(stream_url)
    except Exception:
        return False
    return (
        parsed.scheme in ("http", "https")
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


def _phalang_stream_kv_key(match_id: str) -> str:
    return f"phalang-stream:{quote(match_id, safe='')}"


def _remember_phalang_stream(match_id: str, stream_url: str, built_at: float) -> None:
    with _upcoming_cache_lock:
        if match_id not in _upcoming_cache and len(_upcoming_cache) >= _UPCOMING_CACHE_MAX:
            oldest = min(
                _upcoming_cache,
                key=lambda key: _upcoming_cache[key].get("ts", 0),
            )
            del _upcoming_cache[oldest]
        _upcoming_cache[match_id] = {"url": stream_url, "ts": built_at}


def _persist_phalang_stream_kv(
    match_id: str,
    stream_url: str,
    built_at: float,
    synchronous: bool = False,
) -> None:
    if not _cloudflare_kv_enabled() or not _valid_phalang_stream_url(stream_url):
        return
    args = (_phalang_stream_kv_key(match_id), stream_url, built_at)
    kwargs = {
        "expiration_ttl": PHALANG_STREAM_KV_TTL,
        "timeout": PHALANG_STREAM_KV_TIMEOUT,
    }
    if synchronous:
        _cloudflare_kv_put(*args, **kwargs)
        return
    threading.Thread(
        target=_cloudflare_kv_put,
        args=args,
        kwargs=kwargs,
        daemon=True,
        name=f"kv-write-phalang-{match_id[:24]}",
    ).start()


def _get_phalang_stream_from_kv(match_id: str) -> str:
    if not _cloudflare_kv_enabled():
        return ""
    saved = _cloudflare_kv_get(
        _phalang_stream_kv_key(match_id),
        timeout=PHALANG_STREAM_KV_TIMEOUT,
    )
    if not saved:
        _increment_phalang_resolver_stat("persistent_cache_misses")
        return ""

    stream_url, built_at = saved
    age = time.time() - built_at
    if (
        age < 0
        or age > PHALANG_STREAM_KV_TTL
        or not _valid_phalang_stream_url(stream_url)
    ):
        _increment_phalang_resolver_stat("persistent_cache_misses")
        return ""

    _remember_phalang_stream(match_id, stream_url, built_at)
    _increment_phalang_resolver_stat("persistent_cache_hits")
    return stream_url


def _ensure_background_tasks() -> None:
    global _background_started
    if _background_started:
        return
    with _background_lock:
        if _background_started:
            return
        threading.Thread(target=_prefetch_loop, daemon=True, name="playlist-refresh").start()
        if _get_ping_url():
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
    for key in ("VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_URL"):
        vercel_url = os.environ.get(key, "").strip()
        if vercel_url:
            if not vercel_url.startswith(("http://", "https://")):
                vercel_url = f"https://{vercel_url}"
            return vercel_url.rstrip("/")
    if has_request_context():
        forwarded_host = request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
        host = forwarded_host or request.host
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
        proto = forwarded_proto or request.scheme or "https"
        if host and "localhost" not in host and "127.0.0.1" not in host:
            return f"{proto}://{host}".rstrip("/")
    return f"http://localhost:{os.environ.get('PORT', 5000)}"

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
    resp = _http_session.get(url, headers=_GIOVANG_HEADERS, timeout=GIOVANG_API_TIMEOUT)
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


def _giovang_fixture_id(match: dict) -> str:
    return str(match.get("id") or match.get("fi") or "").strip()


def _giovang_detail_is_due(match: dict, now: float) -> bool:
    """Return whether this fixture is live or close enough to kickoff to enrich."""
    status_code = str(match.get("status_code") or "").upper().strip()
    if bool(match.get("is_live")) or status_code in {"LIVE", "1H", "2H", "HT", "PEN", "ET"}:
        return True

    try:
        start_time = float(match.get("time_start"))
    except (TypeError, ValueError):
        return False

    return now <= start_time <= now + GIOVANG_DETAIL_WINDOW_SECONDS


def _giovang_cached_detail(fixture_id: str) -> tuple[dict | None, bool]:
    """Return cached detail and whether it is fresh enough to avoid a fetch."""
    if not fixture_id:
        return None, False
    with _giovang_detail_cache_lock:
        cached = _giovang_detail_cache.get(fixture_id)
        if not cached:
            return None, False
        detail = cached.get("match")
        fetched_at = float(cached.get("fetched_at") or 0)
        if not isinstance(detail, dict):
            return None, False
        fresh = time.time() - fetched_at < GIOVANG_DETAIL_CACHE_TTL
        return detail, fresh


def _store_giovang_detail(fixture_id: str, detail: dict) -> None:
    if not fixture_id or not isinstance(detail, dict):
        return
    with _giovang_detail_cache_lock:
        # Keep the cache bounded in case the upstream keeps adding fixture IDs.
        if fixture_id not in _giovang_detail_cache and len(_giovang_detail_cache) >= 512:
            oldest_id = min(
                _giovang_detail_cache,
                key=lambda key: _giovang_detail_cache[key].get("fetched_at", 0),
            )
            del _giovang_detail_cache[oldest_id]
        _giovang_detail_cache[fixture_id] = {
            "match": detail,
            "fetched_at": time.time(),
        }


def _merge_giovang_schedule_match(schedule: dict, detail: dict) -> dict:
    """Keep the detail stream while applying the newest schedule/status fields."""
    merged = dict(detail)
    for key, value in schedule.items():
        # List responses currently omit streams, but do not let an empty list
        # erase a stream held in the detail cache.
        if key == "blv" and not _pick_giovang_streams(schedule):
            continue
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


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
    now = time.time()
    detail_cache_hits = 0
    reused_outside_window = 0
    skipped_without_cache = 0
    for match in candidates:
        if _pick_giovang_streams(match):
            enriched.append(match)
            continue

        fixture_id = _giovang_fixture_id(match)
        cached_detail, cache_is_fresh = _giovang_cached_detail(fixture_id)
        detail_is_due = _giovang_detail_is_due(match, now)
        if cached_detail and (cache_is_fresh or not detail_is_due):
            enriched.append(_merge_giovang_schedule_match(match, cached_detail))
            if cache_is_fresh:
                detail_cache_hits += 1
            else:
                reused_outside_window += 1
        elif detail_is_due:
            needs_detail.append((match, cached_detail))
        else:
            skipped_without_cache += 1

    def fetch_detail(match: dict) -> tuple[str, dict]:
        fixture_id = _giovang_fixture_id(match)
        detail_data = _fetch_giovang_json(f"{host}/api/fixtures/{quote(str(fixture_id), safe='')}")
        detail = detail_data.get("response")
        return fixture_id, detail if isinstance(detail, dict) else match

    errors = 0
    if needs_detail:
        with ThreadPoolExecutor(max_workers=min(8, len(needs_detail))) as pool:
            futures = {
                pool.submit(fetch_detail, match): (match, cached_detail)
                for match, cached_detail in needs_detail
            }
            for future in as_completed(futures):
                match, cached_detail = futures[future]
                try:
                    fixture_id, detail = future.result()
                except Exception:
                    errors += 1
                    if cached_detail and _giovang_is_active(cached_detail):
                        enriched.append(_merge_giovang_schedule_match(match, cached_detail))
                    continue
                _store_giovang_detail(fixture_id, detail)
                merged = _merge_giovang_schedule_match(match, detail)
                if _giovang_is_active(merged) and _pick_giovang_streams(merged):
                    enriched.append(merged)

    with _giovang_detail_cache_lock:
        cache_size = len(_giovang_detail_cache)
    with _source_timing_lock:
        _giovang_detail_stats.update({
            "last_requested": len(needs_detail),
            "last_successful": len(needs_detail) - errors,
            "last_errors": errors,
            "last_cache_hits": detail_cache_hits,
            "last_reused_outside_window": reused_outside_window,
            "last_skipped_without_cache": skipped_without_cache,
            "cache_entries": cache_size,
        })

    if candidates and not enriched:
        if errors:
            raise RuntimeError(f"Giờ Vàng fixture details failed for {errors} fixtures")
        raise RuntimeError("Giờ Vàng has no cached streams inside the detail window")
    return enriched

def _giovang_logo(match: dict) -> str:
    values = (
        match.get("type"), match.get("league"), match.get("competition"),
        match.get("tournament"), match.get("category"), match.get("sport"),
    )
    parts = " ".join(
        part for value in values
        if (part := _logo_context_text(value))
    )
    return _match_or_sport_logo(match, "giovang", parts)


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

    def post_query(queries: list[dict], query_or: bool = False) -> list:
        matches = []
        fetched = 0
        total = None

        for page in range(1, PHALANG_QUERY_MAX_PAGES + 1):
            payload = {
                "limit": PHALANG_QUERY_PAGE_SIZE,
                "page": page,
                "order_asc": "start_date",
                "queries": queries,
            }
            if query_or:
                payload["query_or"] = True

            resp = _http_session.post(
                url,
                json=payload,
                headers=_PHALANG_HEADERS,
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise RuntimeError("PhaLang API returned a non-object response")
            results = data.get("data", [])
            if not isinstance(results, list):
                raise RuntimeError("PhaLang API returned an invalid data list")

            page_matches = [match for match in results if isinstance(match, dict)]
            if not page_matches:
                break
            matches.extend(page_matches)
            fetched += len(page_matches)

            try:
                total = max(0, int(data.get("total")))
            except (TypeError, ValueError):
                total = None

            if total is not None and fetched >= total:
                break
            if total is None and len(page_matches) < PHALANG_QUERY_PAGE_SIZE:
                break

        return matches

    # Keep live events separate from featured/upcoming events. A combined OR
    # query can place a full page of old live records ahead of today's fixtures.
    live_query = [
        {"field": "is_live", "type": "equal", "value": True},
    ]
    featured_query = [
        {"field": "is_hot", "type": "equal", "value": True},
        {"field": "is_top", "type": "equal", "value": True},
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        live_future = pool.submit(post_query, live_query)
        featured_future = pool.submit(post_query, featured_query, True)
        matches = live_future.result() + featured_future.result()

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
    if start_str:
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


def _cached_phalang_stream(match_id: str, persist_kv: bool = True) -> str:
    """Resolve one match with short caching and per-match single-flight.

    A popular channel can be requested by many IPTV clients at the same time.
    Only the first request should call PhaLang; the other requests wait for the
    same result instead of creating an upstream burst.
    """
    match_id = str(match_id or "").strip()
    if not match_id:
        return ""

    now = time.time()
    leader = False
    stale_url = ""
    stale_ts = 0
    with _upcoming_cache_lock:
        cached = _upcoming_cache.get(match_id)
        if cached:
            cached_url = str(cached.get("url") or "")
            stale_url = cached_url
            stale_ts = float(cached.get("ts") or 0)
            ttl = _upcoming_cache_ttl if cached_url else _upcoming_miss_ttl
            if now - stale_ts < ttl:
                return cached_url

        event = _upcoming_inflight.get(match_id)
        if event is None:
            event = threading.Event()
            _upcoming_inflight[match_id] = event
            leader = True

    if not leader:
        # Do not hold the cache lock while waiting for the network request.
        # A stale successful URL is safer than turning a burst into 503s.
        event.wait(timeout=12)
        with _upcoming_cache_lock:
            refreshed = _upcoming_cache.get(match_id)
            if refreshed is not None:
                return str(refreshed.get("url") or "")
        return stale_url

    stream_url = ""
    try:
        stream_url = _get_phalang_stream_from_kv(match_id)
        fetched_from_upstream = not bool(stream_url)
        if fetched_from_upstream:
            stream_url = _fetch_phalang_stream(match_id)
            if not _valid_phalang_stream_url(stream_url):
                stream_url = ""

        if stream_url:
            if fetched_from_upstream:
                cache_ts = time.time()
                _remember_phalang_stream(match_id, stream_url, cache_ts)
                if persist_kv:
                    _persist_phalang_stream_kv(
                        match_id, stream_url, cache_ts, synchronous=False
                    )
            else:
                with _upcoming_cache_lock:
                    cached = _upcoming_cache.get(match_id) or {}
                    cache_ts = float(cached.get("ts") or time.time())
                _remember_phalang_stream(match_id, stream_url, cache_ts)
        elif stale_url and 0 <= time.time() - stale_ts <= PHALANG_STREAM_KV_TTL:
            # A recently successful link is preferable to a 503 during a brief
            # upstream resolver outage. Keep its original age so it is retried.
            stream_url = stale_url
            _remember_phalang_stream(match_id, stream_url, stale_ts)
        else:
            with _upcoming_cache_lock:
                if (
                    match_id not in _upcoming_cache
                    and len(_upcoming_cache) >= _UPCOMING_CACHE_MAX
                ):
                    oldest = min(
                        _upcoming_cache,
                        key=lambda key: _upcoming_cache[key].get("ts", 0),
                    )
                    del _upcoming_cache[oldest]
                _upcoming_cache[match_id] = {
                    "url": "",
                    "ts": time.time(),
                }
        return stream_url
    finally:
        with _upcoming_cache_lock:
            event = _upcoming_inflight.pop(match_id, None)
            if event is not None:
                event.set()


def _get_cached_phalang_stream(match_id: str) -> str:
    """Read a still-valid prewarmed URL without triggering another API call."""
    match_id = str(match_id or "").strip()
    if not match_id:
        return ""

    with _upcoming_cache_lock:
        cached = _upcoming_cache.get(match_id)
        if not cached:
            return ""
        stream_url = str(cached.get("url") or "")
        if not stream_url:
            return ""
        if time.time() - float(cached.get("ts") or 0) >= _upcoming_cache_ttl:
            return ""
        return stream_url


def _phalang_direct_stream_url(stream_url: str) -> str:
    """Attach IPTV client request headers to a direct prewarmed HLS URL."""
    stream_url = str(stream_url or "").strip()
    if not stream_url:
        return ""
    if "|" in stream_url:
        return stream_url
    return (
        f"{stream_url}|Referer={PHALANG_LIVE_FRONTEND.rstrip('/')}/"
        f"&User-Agent=Mozilla/5.0"
    )


def _phalang_stream_url(match_id: str) -> str:
    """Return a stable local URL that resolves the upstream link on demand."""
    resolver = f"{_get_server_base_url()}/phalang/live/{quote(str(match_id), safe='')}"
    return (
        f"{resolver}|Referer={PHALANG_LIVE_FRONTEND.rstrip('/')}/"
        f"&User-Agent=Mozilla/5.0"
    )


def _phalang_prewarm_candidates(matches: list) -> list[str]:
    """Select a small live/near-kickoff set for bounded link prewarming."""
    if PHALANG_PREWARM_MAX_MATCHES <= 0:
        return []

    now = time.time()
    candidates = []
    for match in matches:
        match_id = str(match.get("id") or "").strip()
        if not match_id or not _phalang_is_active(match):
            continue

        if bool(match.get("is_live")):
            candidates.append((0, 0, match_id))
            continue

        start_str = match.get("start_date", "")
        try:
            start_ts = datetime.fromisoformat(start_str).timestamp()
        except Exception:
            continue
        seconds_until_start = start_ts - now
        if 0 <= seconds_until_start <= PHALANG_PREWARM_WINDOW:
            candidates.append((1, seconds_until_start, match_id))

    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return [match_id for _, _, match_id in candidates[:PHALANG_PREWARM_MAX_MATCHES]]


def _phalang_hls_url_allowed(url: str) -> bool:
    try:
        parsed = urlsplit(str(url or "").strip())
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except Exception:
        return False
    if (
        parsed.scheme != "https"
        or not host
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    return any(
        host == allowed or host.endswith(f".{allowed}")
        for allowed in PHALANG_HLS_ALLOWED_HOSTS
    )


def _fetch_phalang_manifest(url: str):
    """Fetch one allowlisted playlist, following only safe CDN redirects."""
    current_url = url
    for _ in range(4):
        if not _phalang_hls_url_allowed(current_url):
            return None
        response = None
        try:
            response = _phalang_prewarm_session.get(
                current_url,
                headers=_PHALANG_HEADERS,
                timeout=(2, PHALANG_HLS_PREWARM_TIMEOUT),
                allow_redirects=False,
                stream=True,
            )
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location", "")
                if not location:
                    return None
                current_url = urljoin(current_url, location)
                continue
            if response.status_code != 200:
                return None
            body = response.raw.read(65536, decode_content=True)
            if not body.lstrip().startswith(b"#EXTM3U"):
                return None
            return response.url or current_url, body
        except Exception:
            return None
        finally:
            if response is not None:
                response.close()
    return None


def _first_phalang_variant(manifest: bytes, base_url: str) -> str:
    text = manifest.decode("utf-8", errors="replace")
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip().startswith("#EXT-X-STREAM-INF"):
            continue
        for candidate in lines[index + 1:]:
            candidate = candidate.strip()
            if candidate and not candidate.startswith("#"):
                variant_url = urljoin(base_url, candidate)
                return variant_url if _phalang_hls_url_allowed(variant_url) else ""
    return ""


def _prewarm_phalang_manifest(stream_url: str) -> bool:
    started = time.monotonic()
    success = False
    _increment_phalang_resolver_stat("manifest_prewarm_attempts")
    try:
        if not _phalang_hls_url_allowed(stream_url):
            return False
        master = _fetch_phalang_manifest(stream_url)
        if not master:
            return False
        master_url, manifest = master
        variant_url = _first_phalang_variant(manifest, master_url)
        if variant_url:
            success = bool(_fetch_phalang_manifest(variant_url))
        else:
            # The first document is already a media playlist.
            success = True
        return success
    except Exception:
        return False
    finally:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        with _source_timing_lock:
            if success:
                _phalang_resolver_stats["manifest_prewarm_successes"] += 1
            else:
                _phalang_resolver_stats["manifest_prewarm_errors"] += 1
            _phalang_resolver_stats["manifest_prewarm_last_ms"] = elapsed_ms
            _phalang_resolver_stats["manifest_prewarm_last_at"] = time.time()


def _prewarm_phalang_match(match_id: str) -> bool:
    stream_url = _cached_phalang_stream(match_id, persist_kv=False)
    if not stream_url:
        return False
    with _upcoming_cache_lock:
        cached = _upcoming_cache.get(match_id) or {}
        built_at = float(cached.get("ts") or time.time())
    _persist_phalang_stream_kv(
        match_id, stream_url, built_at, synchronous=True
    )
    return _prewarm_phalang_manifest(stream_url)


def _prewarm_phalang_matches(matches: list) -> None:
    """Warm only the most relevant links without blocking playlist delivery."""
    match_ids = _phalang_prewarm_candidates(matches)
    if not match_ids:
        return

    futures = [
        _phalang_prewarm_pool.submit(_prewarm_phalang_match, match_id)
        for match_id in match_ids
    ]
    deadline = time.monotonic() + PHALANG_PREWARM_WAIT
    for future in futures:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            # Waiting briefly makes the first channel open faster while the
            # bounded worker pool continues unfinished requests in the warm
            # process. It never waits for the full upstream timeout.
            future.result(timeout=remaining)
        except Exception:
            pass


def _phalang_logo(match: dict) -> str:
    values = (
        match.get("desc"), match.get("title"), match.get("type"),
        match.get("league"), match.get("tournament"),
        match.get("competition"), match.get("category"),
        match.get("sport"), match.get("tags"),
    )
    parts = " ".join(
        part for value in values
        if (part := _logo_context_text(value))
    )
    return _match_or_sport_logo(match, "phalang", parts)


def _build_phalang_lines(matches: list, use_prewarmed_live: bool = False) -> list:
    try:
        matches = sorted(matches, key=lambda m: m.get("start_date") or "")
    except Exception:
        pass

    active = [m for m in matches if _phalang_is_active(m)]

    lines = []
    for match in active:
        mid = str(match.get("id") or "").strip()
        if not mid:
            continue
        is_live = bool(match.get("is_live"))

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
        direct_url = (
            _phalang_direct_stream_url(_get_cached_phalang_stream(mid))
            if use_prewarmed_live and is_live
            else ""
        )
        if direct_url:
            # LIVE matches use the already-prewarmed HLS URL, avoiding one
            # resolver round trip when the IPTV client starts playback.
            lines.append(direct_url)
        else:
            # Keep a resolver fallback for matches that were not prewarmed or
            # whose direct URL was unavailable.
            lines.append(_phalang_stream_url(mid))
    return lines


def _fetch_phalang_playlist_lines() -> list:
    matches = _fetch_phalang_matches()
    _prewarm_phalang_matches(matches)
    return _build_phalang_lines(matches, use_prewarmed_live=True)

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
        "giovang": lambda: _build_giovang_lines(_fetch_giovang_matches()),
        "phalang": _fetch_phalang_playlist_lines,
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


def _repair_phalang_playlist_urls(text: str) -> str:
    """Migrate resolver URLs persisted before the public-host fix."""
    if not text or not has_request_context():
        return text

    base_url = _get_server_base_url()
    if "localhost" in base_url or "127.0.0.1" in base_url:
        return text

    local_resolver = re.compile(
        r"^https?://(?:localhost|127\.0\.0\.1)(?::\d+)?/"
        r"(?:upcoming|phalang/live)/([^|\s]+)(?:\|(.*))?$"
    )
    default_options = (
        f"Referer={PHALANG_LIVE_FRONTEND.rstrip('/')}/"
        f"&User-Agent=Mozilla/5.0"
    )
    repaired = []
    changed = False
    for line in text.splitlines():
        match = local_resolver.match(line.strip())
        if match:
            options = match.group(2) or default_options
            line = f"{base_url}/phalang/live/{match.group(1)}|{options}"
            changed = True
        repaired.append(line)

    if not changed:
        return text
    return "\n".join(repaired) + ("\n" if text.endswith("\n") else "")


# ══════════════════════════════════════════════════════════════════════════════
def _repair_match_logo_urls(text: str) -> str:
    """Migrate cached logo links and invalidate rendered-image caches."""
    if not text:
        return text
    migrated = re.sub(r"/match-logo\.svg(?=\?)", "/match-logo.png", text)

    def bump_version(match: re.Match) -> str:
        logo_url = match.group(1)
        version = re.search(r"([?&]v=)[^&]*", logo_url)
        if version:
            return (
                logo_url[:version.start()]
                + version.group(1)
                + MATCH_LOGO_RENDER_VERSION
                + logo_url[version.end():]
            )
        separator = "&" if "?" in logo_url else "?"
        return f"{logo_url}{separator}v={MATCH_LOGO_RENDER_VERSION}"

    return re.sub(
        r"(/match-logo\.png(?:\?[^\"\s]*)?)",
        bump_version,
        migrated,
    )


#  Cache helpers — build compressed + ETag
# ══════════════════════════════════════════════════════════════════════════════

def _pack(text: str, built_at: float | None = None) -> dict:
    raw = text.encode("utf-8")
    gz = gzip.compress(raw, compresslevel=6)
    return {"content": raw, "gz": gz, "built_at": built_at or time.time()}


def _store_local(key: str, text: str, built_at: float | None = None):
    packed = _pack(text, built_at)
    entry = _playlist_cache[key]
    with entry["lock"]:
        entry.update(packed)
    return packed["built_at"]


def _store(key: str, text: str):
    built_at = _store_local(key, text)
    if _cloudflare_kv_enabled():
        _persist_cloudflare_kv(key, text, built_at)


_SOURCE_GROUP_TITLES = {
    "giovang": "Giờ Vàng TV",
    "phalang": "PhaLang TV",
}


def _extract_source_lines_from_combined(text: str, key: str) -> list:
    """Recover one source from a previously cached combined M3U."""
    title = _SOURCE_GROUP_TITLES.get(key)
    if not title:
        return []
    lines = text.splitlines()
    selected = []
    marker = f'group-title="{title}"'
    for index, line in enumerate(lines):
        if not line.startswith("#EXTINF") or marker not in line:
            continue
        selected.append(line)
        if index + 1 < len(lines):
            selected.append(lines[index + 1])
    return selected


def _get_previous_source_lines(key: str) -> list:
    """Keep source data through an upstream outage, including after cold start."""
    # A combined KV hydrate does not automatically populate per-source memory.
    # Try the source-specific KV value before falling back to the combined body.
    _hydrate_from_cloudflare_kv(key)
    previous = _get_entry(key)
    if previous.get("content"):
        lines = previous["content"].decode("utf-8", errors="replace").splitlines()
        lines = [line for line in lines if not line.startswith("#EXTM3U")]
        if any(line.startswith("#EXTINF") for line in lines):
            return lines

    combined = _get_entry("combined")
    if combined.get("content"):
        text = combined["content"].decode("utf-8", errors="replace")
        return _extract_source_lines_from_combined(text, key)
    return []


def _is_removed_group_extinf(line: str) -> bool:
    if not line.startswith("#EXTINF"):
        return False
    match = re.search(r"\bgroup-title\s*=\s*([\"'])(.*?)\1", line, re.IGNORECASE)
    if not match:
        return False
    group = match.group(2).strip().lower().replace("đ", "d")
    group = unicodedata.normalize("NFKD", group).encode("ascii", "ignore").decode("ascii")
    return group.startswith("khan dai") or group == "phaohoa"


def _strip_removed_groups(text: str) -> str:
    """Remove Khán Đài entries, including their stream/options, from legacy M3U caches."""
    if not text:
        return text
    output = []
    skipping_entry = False
    for line in text.splitlines():
        if line.startswith("#EXTINF"):
            skipping_entry = _is_removed_group_extinf(line)
            if not skipping_entry:
                output.append(line)
            continue
        if skipping_entry:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            skipping_entry = False
            continue
        output.append(line)
    cleaned = "\n".join(output)
    return cleaned + ("\n" if text.endswith(("\n", "\r")) else "")


def _repair_combined_from_source_cache(text: str) -> str:
    """Restore missing source sections from source-specific KV after cold start."""
    cleaned_text = _repair_match_logo_urls(_strip_removed_groups(text))
    lines = cleaned_text.splitlines()
    if not lines:
        return cleaned_text

    error_lines = [line for line in lines if line.startswith("# Errors:")]
    body = [line for line in lines if not line.startswith("# Errors:")]
    changed = cleaned_text != text
    for key in ("phalang", "giovang"):
        title = _SOURCE_GROUP_TITLES[key]
        marker = f'group-title="{title}"'
        if any(line.startswith("#EXTINF") and marker in line for line in body):
            continue
        recovered = _get_previous_source_lines(key)
        if recovered:
            body.extend(recovered)
            changed = True

    if not changed:
        return cleaned_text
    return "\n".join(body + error_lines)


# ══════════════════════════════════════════════════════════════════════════════
#  Background pre-fetch (parallel sources)
# ══════════════════════════════════════════════════════════════════════════════

def _refresh_all_playlists():
    _purge_removed_group_cache()
    errors = []

    def fetch_giovang():
        return _refresh_source_playlist("giovang")

    def fetch_phalang():
        return _refresh_source_playlist("phalang")

    def fetch_dekiki():
        return _refresh_source_playlist("dekiki")

    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
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

    giovang_lines   = results.get("giovang",   [])
    phalang_lines   = results.get("phalang",   [])
    dekiki_lines    = results.get("dekiki",    [])

    if not giovang_lines and any(error.startswith("giovang:") for error in errors):
        giovang_lines = _get_previous_source_lines("giovang")
        if giovang_lines:
            errors.append("giovang: kept last successful playlist")

    if not phalang_lines and any(error.startswith("phalang:") for error in errors):
        phalang_lines = _get_previous_source_lines("phalang")
        if phalang_lines:
            errors.append("phalang: kept last successful playlist")

    err_str = "; ".join(errors)

    def count(lines):
        return sum(1 for line in lines if line.startswith("#EXTINF"))

    epg_header = f'#EXTM3U url-tvg="{EPG_URL}" x-tvg-url="{EPG_URL}"'
    all_lines = phalang_lines + giovang_lines + dekiki_lines
    combined_text = epg_header + "\n" + "\n".join(all_lines)
    if err_str:
        combined_text += f"\n# Errors: {err_str}"
    _store("combined", _strip_removed_groups(combined_text))

    _last_counts.update({
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

def _trigger_async_refresh(key: str) -> bool:
    """Refresh stale data in the background while serving the last good body."""
    with _stale_refresh_lock:
        if key in _stale_refresh_keys:
            return False
        _stale_refresh_keys.add(key)

    def worker():
        try:
            if key == "combined":
                _refresh_all_with_lock(blocking=False)
            else:
                _refresh_source_playlist(key)
        except Exception as exc:
            with _source_timing_lock:
                _source_refresh_errors[key] = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            with _stale_refresh_lock:
                _stale_refresh_keys.discard(key)

    threading.Thread(target=worker, daemon=True, name=f"refresh-{key}").start()
    return True

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
            "built_at": entry["built_at"],
        }


def _playlist_refresh_interval(key: str) -> int:
    if key == "phalang":
        return PHALANG_PLAYLIST_INTERVAL
    return PREFETCH_INTERVAL


# ══════════════════════════════════════════════════════════════════════════════
#  Flask routes
# ══════════════════════════════════════════════════════════════════════════════

def _m3u_response(key: str, filename: str) -> Response:
    _ensure_background_tasks()
    entry = _get_entry(key)
    if entry["content"] is None:
        _hydrate_from_cloudflare_kv(key)
        entry = _get_entry(key)
        if key == "combined" and entry["content"] is not None:
            cached_text = entry["content"].decode("utf-8", errors="replace")
            repaired_text = _repair_combined_from_source_cache(cached_text)
            repaired_text = _repair_phalang_playlist_urls(repaired_text)
            repaired_text = _repair_match_logo_urls(repaired_text)
            if repaired_text != cached_text:
                _store("combined", repaired_text)
                entry = _get_entry(key)
    if key in ("phalang", "giovang") and entry["content"] is not None:
        cached_text = entry["content"].decode("utf-8", errors="replace")
        repaired_text = _repair_match_logo_urls(cached_text)
        if key == "phalang":
            repaired_text = _repair_phalang_playlist_urls(repaired_text)
        if repaired_text != cached_text:
            _store(key, repaired_text)
            entry = _get_entry(key)
    refresh_error = ""
    refresh_scheduled = False
    did_refresh = False
    cache_age = time.time() - entry["built_at"] if entry["content"] is not None else None
    refresh_interval = _playlist_refresh_interval(key)
    needs_refresh = entry["content"] is None or cache_age >= refresh_interval

    if needs_refresh and entry["content"] is None:
        # There is no usable body yet, so the first request must populate it.
        try:
            if key == "combined":
                _refresh_all_with_lock(blocking=True)
            else:
                _refresh_source_playlist(key)
            did_refresh = True
        except Exception as e:
            refresh_error = f"{type(e).__name__}: {e}"[:300]
        entry = _get_entry(key)
        if entry["content"] is None:
            status = 500 if key == "combined" else 502
            return Response(
                f"Upstream error for {key}: {refresh_error or 'playlist cache is not ready'}",
                status=status,
                mimetype="text/plain",
            )
    elif needs_refresh:
        # Serve the last good playlist immediately; refresh it once in background.
        refresh_scheduled = _trigger_async_refresh(key)

    entry = _get_entry(key)
    use_gzip = "gzip" in request.headers.get("Accept-Encoding", "") and entry["gz"] is not None
    body = entry["gz"] if use_gzip else entry["content"]
    resp = Response(body, mimetype="application/x-mpegurl")
    resp.headers["Cache-Control"]       = _PLAYLIST_CACHE_CONTROL
    resp.headers["Pragma"]              = "no-cache"
    resp.headers["Expires"]             = "0"
    resp.headers["Surrogate-Control"]   = "no-store"
    resp.headers["CDN-Cache-Control"]   = "no-store"
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.headers["Vary"]                = "Accept-Encoding"
    resp.headers["X-Playlist-Built-At"] = str(int(entry["built_at"]))
    if did_refresh:
        cache_state = "refreshed"
    elif refresh_scheduled:
        cache_state = "stale-refreshing"
    elif needs_refresh:
        cache_state = "stale"
    else:
        cache_state = "memory"
    resp.headers["X-Playlist-Cache"] = cache_state
    if refresh_error:
        resp.headers["X-Playlist-Refresh-Error"] = refresh_error
    if use_gzip:
        resp.headers["Content-Encoding"] = "gzip"
    return resp

@app.route("/live.m3u")
def live_m3u():
    return _m3u_response("combined", "live.m3u")

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
        giovang_detail_stats = dict(_giovang_detail_stats)
        phalang_resolver_stats = dict(_phalang_resolver_stats)
        cloudflare_kv_backoff_seconds = max(
            0, int(_cloudflare_kv_backoff_until - time.time())
        )
    with _upcoming_cache_lock:
        phalang_resolver_cache = {
            "cached_matches": len(_upcoming_cache),
            "inflight_matches": len(_upcoming_inflight),
        }

    cached_source_bodies = {
        key: _get_entry(key)["content"] is not None
        for key in ("giovang", "phalang", "dekiki")
    }

    def source_state(key: str) -> str:
        error = source_refresh_errors.get(key, "")
        count = _last_counts.get(key, 0)
        if key == "phalang" and not count:
            if error:
                return "stale" if cached_source_bodies[key] else "error"
            return "empty"
        if error:
            return "stale" if count or cached_source_bodies[key] else "error"
        return "ok" if count or cached_source_bodies[key] else "empty"

    source_states = {
        key: source_state(key)
        for key in ("giovang", "phalang", "dekiki")
    }
    has_source_errors = any(state == "error" for state in source_states.values())
    has_empty_sources = any(state == "empty" for state in source_states.values())
    has_stale_sources = any(state == "stale" for state in source_states.values())
    has_unclassified_error = bool(_last_counts.get("last_error")) and not (
        has_source_errors or has_empty_sources or has_stale_sources
    )
    if has_source_errors or has_empty_sources or has_unclassified_error:
        health_state = "error"
    elif has_stale_sources:
        health_state = "degraded"
    else:
        health_state = "ok"

    return jsonify({
        # Keep "ok" for existing monitors. A stale playlist is intentionally
        # not fully healthy, but the explicit state explains why it is usable.
        "ok":           health_state == "ok",
        "state":        health_state,
        "degraded":     health_state == "degraded",
        "serving_cached_playlist": any(cached_source_bodies.values()),
        "refreshed_at": ra_vn,
        "next_refresh_in_seconds": next_s,
        "last_error":   _last_counts.get("last_error", ""),
        "source_refresh_ms": source_refresh_ms,
        "source_refresh_errors": source_refresh_errors,
        "persistent_cache": {
            "provider": "cloudflare-kv",
            "enabled": _cloudflare_kv_enabled(),
            "hydrated_keys": sorted(_kv_hydrated_keys),
            "last_write_at": _cloudflare_kv_last_write_at or None,
            "last_error": _cloudflare_kv_last_error,
            "backoff_remaining_seconds": cloudflare_kv_backoff_seconds,
        },
        "channels": {
            "total":      sum(_last_counts.get(k, 0) for k in ("giovang", "phalang", "dekiki")),
            "giovang_tv": _last_counts.get("giovang", 0),
            "phalang_tv": _last_counts.get("phalang", 0),
            "dekiki_tv":  _last_counts.get("dekiki",  0),
        },
        "sources": {
            "giovang_tv": {"api": _giovang_api_cache.get("host"), "status": source_states["giovang"]},
            "phalang_tv": {"api": PHALANG_API_URL,                 "status": source_states["phalang"]},
            "dekiki_tv":  {"api": "github-static",                "status": source_states["dekiki"]},
        },
        "giovang_detail": {
            "window_seconds": GIOVANG_DETAIL_WINDOW_SECONDS,
            "cache_ttl_seconds": GIOVANG_DETAIL_CACHE_TTL,
            **giovang_detail_stats,
        },
        "phalang_resolver": {
            "positive_cache_ttl_seconds": _upcoming_cache_ttl,
            "negative_cache_ttl_seconds": _upcoming_miss_ttl,
            "persistent_cache_ttl_seconds": PHALANG_STREAM_KV_TTL,
            "prewarm_window_seconds": PHALANG_PREWARM_WINDOW,
            "manifest_prewarm_timeout_seconds": PHALANG_HLS_PREWARM_TIMEOUT,
            **phalang_resolver_stats,
            **phalang_resolver_cache,
        },
    })

@app.route("/ping")
def ping():
    return Response("OK", mimetype="text/plain")

def _phalang_live_response(match_id: str):
    stream_url = _cached_phalang_stream(match_id)
    if not stream_url:
        response = Response(
            "Tran chua bat dau / Match not started",
            mimetype="text/plain",
            status=503,
        )
        response.headers["Cache-Control"] = "no-store, no-cache, max-age=0, private"
        response.headers["Retry-After"] = str(_upcoming_miss_ttl)
        return response

    # The M3U resolver line carries IPTV request options. Keep Location a
    # standard HLS URL; putting "|" options into a redirect would be encoded
    # by WSGI and become part of the upstream URL.
    response = Response(status=302)
    response.headers["Location"] = stream_url
    response.headers["Cache-Control"] = "no-store, no-cache, max-age=0, private"
    response.headers["Surrogate-Control"] = "no-store"
    response.headers["CDN-Cache-Control"] = "no-store"
    return response


_LOGO_IMAGE_ALLOWED_HOSTS = frozenset({
    "cdn-img.thethao247.vn",
    "cdn.keobongvip.digital",
    "cdn.jsdelivr.net",
    "img0.aiscore.com",
    "imgts.sportpulseapiz.com",
    "images.keovip88.net",
    "static.wikia.nocookie.net",
    "static.flashscore.com",
    "thumb.wikimedia.org",
    "upload.wikimedia.org",
    "encrypted-tbn0.gstatic.com",
})
_LOGO_IMAGE_MAX_BYTES = 512 * 1024
_LOGO_IMAGE_MAX_PIXELS = 4_000_000
_LOGO_IMAGE_CACHE_MAXSIZE = 96
_LOGO_IMAGE_CACHE_TTL_SECONDS = 6 * 60 * 60
_LOGO_IMAGE_NEGATIVE_CACHE_TTL_SECONDS = 30
_logo_asset_cache: dict[str, tuple[float, bytes]] = {}
_logo_asset_fetch_status: dict[str, str] = {}
_logo_asset_cache_lock = threading.Lock()


def _fetch_logo_asset(url: str, diagnostics: list[str] | None = None) -> bytes:
    """Fetch only known public logo CDNs; never proxy arbitrary URL parameters."""
    def record(status: str) -> None:
        if diagnostics is not None:
            diagnostics.append(status)

    safe_url = _safe_logo_source(url)
    if not safe_url:
        record("unsafe-url")
        return b""
    try:
        parsed = urlsplit(safe_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme.lower() != "https"
            or host not in _LOGO_IMAGE_ALLOWED_HOSTS
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
        ):
            record("blocked-host")
            return b""
        headers = {
            "User-Agent": _http_session.headers.get(
                "User-Agent", "Mozilla/5.0"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        if host == "imgts.sportpulseapiz.com":
            headers["Referer"] = f"{PHALANG_FRONTEND_URL.rstrip('/')}/"
        elif host in {"cdn.keobongvip.digital", "images.keovip88.net"}:
            headers["Referer"] = f"{GIOVANG_FRONTEND_URL.rstrip('/')}/"
        for asset_url in _logo_asset_candidates(safe_url):
            for attempt in range(2):
                client = _http_session
                close_client = False
                if attempt:
                    client = requests.Session()
                    client.verify = _http_session.verify
                    client.trust_env = _http_session.trust_env
                    client.headers.update(headers)
                    client.headers["Connection"] = "close"
                    close_client = True
                try:
                    with client.get(
                        asset_url,
                        headers=headers,
                        timeout=(2.5, 4),
                        allow_redirects=False,
                        stream=True,
                    ) as response:
                        if response.status_code != 200:
                            record(f"http-{response.status_code}")
                            continue
                        content_type = response.headers.get("Content-Type", "")
                        content_type = content_type.split(";", 1)[0].strip().lower()
                        if content_type not in {
                            "image/png", "image/jpeg", "image/webp", "image/gif"
                        }:
                            record("unsupported-content-type")
                            continue
                        length = response.headers.get("Content-Length")
                        if length and int(length) > _LOGO_IMAGE_MAX_BYTES:
                            record("asset-too-large")
                            continue
                        content = bytearray()
                        for chunk in response.iter_content(chunk_size=32 * 1024):
                            if chunk:
                                content.extend(chunk)
                                if len(content) > _LOGO_IMAGE_MAX_BYTES:
                                    break
                        if content and len(content) <= _LOGO_IMAGE_MAX_BYTES:
                            record("ok")
                            return bytes(content)
                        record("empty-or-too-large-body")
                except Exception as exc:
                    status = type(exc).__name__
                    record(f"retry-{status}" if not attempt else f"request-{status}")
                finally:
                    if close_client:
                        client.close()
        if diagnostics is not None and not diagnostics:
            record("no-candidate")
        return b""
    except Exception as exc:
        record(f"fetch-{type(exc).__name__}")
        return b""


def _download_logo_asset(url: str) -> bytes:
    """Cache valid logos briefly and retry transient failures instead of pinning them."""
    safe_url = _safe_logo_source(url)
    if not safe_url:
        return b""

    now = time.monotonic()
    with _logo_asset_cache_lock:
        cached = _logo_asset_cache.get(safe_url)
        if cached is not None:
            expires_at, asset = cached
            if expires_at > now:
                _logo_asset_cache.pop(safe_url)
                _logo_asset_cache[safe_url] = cached
                return asset
            _logo_asset_cache.pop(safe_url, None)

    diagnostics: list[str] = []
    asset = _fetch_logo_asset(safe_url, diagnostics)
    ttl = (
        _LOGO_IMAGE_CACHE_TTL_SECONDS
        if asset
        else _LOGO_IMAGE_NEGATIVE_CACHE_TTL_SECONDS
    )
    with _logo_asset_cache_lock:
        _logo_asset_cache.pop(safe_url, None)
        _logo_asset_cache[safe_url] = (time.monotonic() + ttl, asset)
        _logo_asset_fetch_status[safe_url] = (
            "ok" if asset else ",".join(diagnostics[-3:]) or "no-result"
        )
        while len(_logo_asset_fetch_status) > _LOGO_IMAGE_CACHE_MAXSIZE:
            _logo_asset_fetch_status.pop(next(iter(_logo_asset_fetch_status)))
        while len(_logo_asset_cache) > _LOGO_IMAGE_CACHE_MAXSIZE:
            _logo_asset_cache.pop(next(iter(_logo_asset_cache)))
    return asset


def _logo_asset_fetch_status_for(url: str) -> str:
    with _logo_asset_cache_lock:
        return _logo_asset_fetch_status.get(url, "unknown")


def _logo_asset_candidates(url: str) -> tuple[str, ...]:
    """Prefer larger known-provider logos, then fall back to the source URL."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
    except Exception:
        return (url,)

    if host == "img0.aiscore.com":
        match = re.search(r"(?i)(\.(?:png|jpe?g|webp))!w(\d+)$", parsed.path)
        if not match or int(match.group(2)) >= 300:
            return (url,)
        high_resolution_path = (
            parsed.path[:match.start()] + match.group(1) + "!w300"
        )
    elif host == "thumb.wikimedia.org":
        match = re.search(r"/(\d+)px-[^/]+$", parsed.path)
        if not match or int(match.group(1)) >= 960:
            return (url,)
        high_resolution_path = (
            parsed.path[:match.start(1)] + "960" + parsed.path[match.end(1):]
        )
    else:
        return (url,)

    high_resolution_url = parsed._replace(path=high_resolution_path).geturl()
    return (high_resolution_url, url)


def _logo_initials(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(name or ""))
    ascii_name = "".join(char for char in normalized if not unicodedata.combining(char))
    words = re.findall(r"[A-Za-z0-9]+", ascii_name)
    return "".join(word[0] for word in words[:2]).upper() or "?"


def _match_logo_font(size: int):
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def _draw_match_logo_badge(canvas, center, diameter: int, url: str, name: str) -> str:
    cx, cy = center
    radius = diameter // 2
    draw = ImageDraw.Draw(canvas)
    asset = _download_logo_asset(url) if url else b""
    render_status = (
        "no-source"
        if not url
        else f"fetch-{_logo_asset_fetch_status_for(url)}"
    )
    if asset:
        try:
            with Image.open(BytesIO(asset)) as source:
                if (
                    source.format not in {"PNG", "JPEG", "WEBP", "GIF"}
                    or source.width <= 0
                    or source.height <= 0
                    or source.width * source.height > _LOGO_IMAGE_MAX_PIXELS
                ):
                    raise ValueError("unsupported logo image")
                source.load()
                logo = ImageOps.contain(
                    source.convert("RGBA"),
                    (diameter, diameter),
                    method=Image.Resampling.LANCZOS,
                )
            canvas.alpha_composite(
                logo,
                (int(cx - logo.width / 2), int(cy - logo.height / 2)),
            )
            return "ok"
        except Exception as exc:
            render_status = f"decode-{type(exc).__name__}"
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill=(241, 243, 245, 255),
    )
    initials = _logo_initials(name)
    font = _match_logo_font(max(28, diameter // 3))
    bounds = draw.textbbox((0, 0), initials, font=font)
    text_width = bounds[2] - bounds[0]
    text_height = bounds[3] - bounds[1]
    draw.text(
        (cx - text_width / 2 - bounds[0], cy - text_height / 2 - bounds[1]),
        initials,
        font=font,
        fill=(24, 32, 48, 255),
    )
    return render_status


def _build_match_logo_png(home_url: str, league_url: str, away_url: str,
                          home_name: str, league_name: str, away_name: str,
                          logo_status: list[str] | None = None) -> bytes:
    width, height = 1920, 1080
    canvas = Image.new("RGBA", (width, height), (11, 11, 17, 255))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (36, 36, width - 36, height - 36),
        radius=120,
        fill=(255, 255, 255, 255),
    )
    blue = (5, 128, 245, 255)
    cyan = (5, 203, 193, 255)
    frame_accents = (
        (blue, ((120, 50), (740, 50), (704, 84), (86, 84))),
        (cyan, ((1100, 50), (1560, 50), (1526, 84), (1066, 84))),
        (blue, ((1732, 50), (1828, 50), (1796, 84), (1700, 84))),
        (blue, ((86, 996), (600, 996), (568, 1030), (120, 1030))),
        (cyan, ((1140, 996), (1828, 996), (1796, 1030), (1106, 1030))),
    )
    for color, points in frame_accents:
        draw.polygon(points, fill=color)
    status = [
        _draw_match_logo_badge(canvas, (400, 540), 560, home_url, home_name),
        _draw_match_logo_badge(canvas, (960, 540), 360, league_url, league_name),
        _draw_match_logo_badge(canvas, (1520, 540), 560, away_url, away_name),
    ]
    if logo_status is not None:
        logo_status.extend(status)
    output = BytesIO()
    canvas.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()


@app.route("/match-logo.png")
def match_logo_png():
    """Return a raster match mark for IPTV clients that cannot render SVG."""
    args = request.args
    logo_status: list[str] = []
    png = _build_match_logo_png(
        _safe_logo_source(args.get("home")),
        _safe_logo_source(args.get("league")),
        _safe_logo_source(args.get("away")),
        args.get("home_name", "Home")[:80],
        args.get("league_name", "League")[:80],
        args.get("away_name", "Away")[:80],
        logo_status,
    )
    response = Response(png, mimetype="image/png")
    response.headers["Cache-Control"] = (
        "public, max-age=60, s-maxage=60, stale-while-revalidate=120"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Match-Logo-Assets"] = ",".join(
        f"{label}={status}"
        for label, status in zip(("home", "league", "away"), logo_status)
    )
    return response


@app.route("/match-logo.svg")
def match_logo_svg():
    """Return a match mark with the home, competition, and away logos."""
    args = request.args
    home_url = _safe_logo_source(args.get("home"))
    league_url = _safe_logo_source(args.get("league"))
    away_url = _safe_logo_source(args.get("away"))
    home_name = html.escape((args.get("home_name") or "Home")[:80], quote=True)
    league_name = html.escape((args.get("league_name") or "League")[:80], quote=True)
    away_name = html.escape((args.get("away_name") or "Away")[:80], quote=True)

    def image_or_initials(url: str, name: str, x: int, y: int, size: int, radius: int) -> str:
        if url:
            safe_url = html.escape(url, quote=True)
            return (
                f'<image href="{safe_url}" xlink:href="{safe_url}" '
                f'x="{x}" y="{y}" width="{size}" height="{size}" '
                'preserveAspectRatio="xMidYMid meet" />'
            )
        words = [word for word in name.replace("-", " ").split() if word]
        initials = "".join(word[0] for word in words[:2]).upper() or "?"
        return (
            f'<circle cx="{x + size // 2}" cy="{y + size // 2}" r="{radius}" '
            'fill="#f1f3f5" />'
            f'<text x="{x + size // 2}" y="{y + size // 2 + 18}" '
            'text-anchor="middle" font-family="Arial,sans-serif" '
            'font-size="52" font-weight="700" fill="#18202c">'
            f'{html.escape(initials, quote=True)}</text>'
        )

    home = image_or_initials(home_url, home_name, 58, 54, 220, 110)
    league = image_or_initials(league_url, league_name, 380, 92, 200, 136)
    away = image_or_initials(away_url, away_name, 682, 54, 220, 110)
    title = html.escape(f"{home_name} - {league_name} - {away_name}", quote=True)
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'viewBox="0 0 960 320" width="960" height="320" role="img">'
        '<rect width="960" height="320" fill="#0b0b10"/>'
        '<rect x="18" y="14" width="924" height="292" rx="36" fill="#ffffff"/>'
        '<polygon points="60,20 370,20 354,34 44,34" fill="#0580f5"/>'
        '<polygon points="550,20 780,20 764,34 534,34" fill="#05cbc1"/>'
        '<polygon points="866,20 914,20 898,34 850,34" fill="#0580f5"/>'
        '<polygon points="44,286 300,286 284,300 60,300" fill="#0580f5"/>'
        '<polygon points="570,286 914,286 898,300 554,300" fill="#05cbc1"/>'
        f"<title>{title}</title>{home}{league}{away}</svg>"
    )
    response = Response(svg, mimetype="image/svg+xml")
    response.headers["Cache-Control"] = "public, max-age=3600"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/phalang/live/<match_id>")
def phalang_live(match_id: str):
    return _phalang_live_response(match_id)


@app.route("/upcoming/<match_id>")
def upcoming(match_id: str):
    # Backward-compatible alias for older playlists already in IPTV clients.
    return _phalang_live_response(match_id)

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

    err = _last_counts.get("last_error", "")
    source_errors = dict(_source_refresh_errors)
    cached_source_bodies = {
        key: _get_entry(key)["content"] is not None
        for key in ("giovang", "phalang", "dekiki")
    }
    source_states = {}
    for key in ("giovang", "phalang", "dekiki"):
        count = _last_counts.get(key, 0)
        source_states[key] = (
            "stale" if source_errors.get(key) and (count or cached_source_bodies[key])
            else "error" if source_errors.get(key) or not cached_source_bodies[key]
            else "ok"
        )
    has_source_errors = any(state == "error" for state in source_states.values())
    has_stale_sources = any(state == "stale" for state in source_states.values())
    has_unclassified_error = bool(err) and not (has_source_errors or has_stale_sources)
    if has_source_errors or has_unclassified_error:
        health_state = "error"
    elif has_stale_sources:
        health_state = "degraded"
    else:
        health_state = "ok"

    if health_state == "error":
        err_html = f'<p style="color:red">❌ Trạng thái: lỗi — {err}</p>' if err else (
            '<p style="color:red">❌ Trạng thái: lỗi — chưa có playlist khả dụng</p>'
        )
    elif health_state == "degraded":
        err_html = (
            '<p style="color:#b45309">⚠️ Trạng thái: degraded — '
            'upstream đang lỗi tạm thời, playlist cache gần nhất vẫn được phục vụ.'
            f' {err}</p>'
        )
    else:
        err_html = '<p style="color:green">✅ Trạng thái: ok — các nguồn đang hoạt động</p>'

    giovang_count  = _last_counts.get("giovang",   0)
    phalang_count  = _last_counts.get("phalang",   0)
    dekiki_count   = _last_counts.get("dekiki",    0)
    total          = giovang_count + phalang_count + dekiki_count

    return (
        "<h2>🎬 IPTV M3U Server</h2>"
        "<h3>📋 Playlist</h3><ul>"
        "<li><a href='/live.m3u'>/live.m3u</a> — Tất cả nguồn gộp lại</li>"
         "<li><a href='/giovang.m3u'>/giovang.m3u</a> — Giờ Vàng TV only</li>"
        "<li><a href='/phalang.m3u'>/phalang.m3u</a> — PhaLang TV only</li>"
        "<li><a href='/dekiki.m3u'>/dekiki.m3u</a> — Kênh TV Việt (dekiki)</li>"
        "</ul>"
        "<h3>📊 Trạng thái</h3>"
        f"<p>📺 Tổng kênh: <strong>{total}</strong>"
        f" | 📡 TV: {dekiki_count})</p>"
        f"<p>🕐 Cập nhật lần cuối: <strong>{dt_str}</strong></p>"
        f"<p>⏳ Cập nhật tiếp theo: <strong>{next_str}</strong></p>"
        f"<p>🟢 Giờ Vàng TV: <strong>{giovang_count} kênh</strong>"
        f"&nbsp;|&nbsp; <code>{_giovang_api_cache['host']}</code></p>"
        f"<p>🟢 PhaLang TV: <strong>{phalang_count} kênh</strong>"
        f"&nbsp;|&nbsp; <code>{PHALANG_API_URL}</code></p>"
         f"<p>📡 Kênh TV (dekiki): <strong>{dekiki_count} kênh</strong></p>"
        f"<p>📻 EPG: <a href='{EPG_URL}' target='_blank'>{EPG_URL}</a></p>"
        f"{err_html}"
        "<h3>⚙️ Tối ưu băng thông</h3><ul>"
        "<li>Gzip nén tự động (giảm ~70% dữ liệu truyền)</li>"
        "<li>Playlist luôn trả HTTP 200 mới, không dùng ETag để tránh client giữ dữ liệu cũ</li>"
        "<li>Cache client max-age=0; Vercel CDN giữ tối đa 30 giây để tránh cold start</li>"
        "<li>1 worker process + 16 threads — cache dùng chung, không fetch trùng lặp</li>"
        "<li>Các nguồn fetch song song (ThreadPoolExecutor)</li>"
        f"<li>Làm mới cache mỗi <strong>{PREFETCH_INTERVAL // 60} phút</strong></li>"
        "</ul>"
         "<h3>⚡ Giờ Vàng TV — Direct Stream</h3><ul>"
         "<li>Schedule và stream được lấy từ JSON public, chi tiết fixture fetch song song</li>"
         "<li>Chỉ hiển thị trận còn hiệu lực và có ít nhất một bình luận viên có stream</li>"
         "</ul>"
         "<h3>⚡ PhaLang TV — Direct Stream</h3><ul>"
          "<li>Playlist dùng resolver ổn định; link stream được lấy khi người dùng mở kênh</li>"
          "<li>Cache per-match + single-flight tránh nhiều người dùng gọi trùng API</li>"
          f"<li>Cache link thành công { _upcoming_cache_ttl }s, cache chưa phát { _upcoming_miss_ttl }s</li>"
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
    return ""

def _self_ping():
    url = _get_ping_url()
    while True:
        time.sleep(SELF_PING_INTERVAL)
        try:
            _http_session.get(url, timeout=15)
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════════════════════
#  Startup
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    _ensure_background_tasks()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

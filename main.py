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
TIEULAM_FRONTEND_URL  = os.environ.get("TIEULAM_FRONTEND", "https://sv2.tieulam.info")
TIEULAM_KNOWN_API_BASE= os.environ.get("TIEULAM_API",      "https://api.tlap17062026.com")
# CDN phát stream — khi source_live=None, build từ stream_key
TIEULAM_STREAM_CDN    = os.environ.get("TIEULAM_CDN",      "https://live.lilive2.eu.cc")
# Kênh IPTV tĩnh
VTV_M3U_URL           = os.environ.get("VTV_M3U_URL", "https://raw.githubusercontent.com/Bacbenny/Verceliptv/refs/heads/main/VTV.m3u")
# ── TieuLam relay (bỏ qua Cloudflare IP-block trên Vercel/Render) ────────────
#
# KHUYẾN NGHỊ: Dùng Replit public relay (không cần secret):
#   TIEULAM_RELAY_URL=https://<your-replit-app>.replit.app/api/tieulam-relay-public
#
# Tuỳ chọn — Cloudflare workers (cần RELAY_SECRET khớp với worker config):
#   TIEULAM_RELAY_URL=https://tieulam-relay.bacbenny95.workers.dev/
#   TIEULAM_RELAY_URL_2=https://dekki.bacbenny95.workers.dev/          ← fallback
#   RELAY_SECRET=<shared-secret>
#
# ── TieuLam 3-tier free setup ────────────────────────────────────────────────
#
# Tầng 1 — GitHub Actions cache (mỗi 30 phút, miễn phí 24/7):
#   TIEULAM_CACHE_URL=https://raw.githubusercontent.com/Bacbenny/Verceliptv/main/data/tieulam_cache.json
#   (mặc định đã set sẵn, không cần cấu hình thêm)
#
# Tầng 2 — Cloudflare Workers relay (miễn phí 100k req/ngày):
#   TIEULAM_RELAY_URL=https://tieulam-relay.bacbenny95.workers.dev/
#   TIEULAM_RELAY_URL_2=https://dekki.bacbenny95.workers.dev/
#   RELAY_SECRET=<same secret in Cloudflare worker>
#
# Tầng 3 — Direct API (chỉ hoạt động khi không bị Cloudflare chặn)
#
# Nếu REPLIT_DOMAINS tồn tại (dev mode) → dùng /api/tieulam-relay-public trên chính Replit này
_replit_domain = os.environ.get("REPLIT_DOMAINS", "").split(",")[0].strip()
_DEFAULT_REPLIT_RELAY = (
    f"https://{_replit_domain}/api/tieulam-relay-public" if _replit_domain else ""
)

TIEULAM_CACHE_URL    = os.environ.get(
    "TIEULAM_CACHE_URL",
    "https://raw.githubusercontent.com/Bacbenny/Verceliptv/main/data/tieulam_cache.json",
)
TIEULAM_CACHE_MAX_AGE = int(os.environ.get("TIEULAM_CACHE_MAX_AGE", "2100"))  # 35 min
TIEULAM_RELAY_URL    = os.environ.get("TIEULAM_RELAY_URL",   _DEFAULT_REPLIT_RELAY)
TIEULAM_RELAY_URL_2  = os.environ.get("TIEULAM_RELAY_URL_2", "")
TIEULAM_RELAY_SECRET = os.environ.get("RELAY_SECRET", "")

# ─── Hội Quán TV config ───────────────────────────────────────────────────────
HOIQUAN_FRONTEND_URL  = os.environ.get("HOIQUAN_FRONTEND", "https://sv2.hoiquan4.live")
HOIQUAN_KNOWN_API_BASE= os.environ.get("HOIQUAN_API",      "https://sv.hoiquantv.xyz/api/v1/external")

# ─── Khán Đài A config ───────────────────────────────────────────────────────
KHANDAIA_FRONTEND_URL   = os.environ.get("KHANDAIA_FRONTEND", "https://tructiep.khandaia.link")
KHANDAIA_KNOWN_API_BASE = os.environ.get("KHANDAIA_API",      "https://sv.khandai-a.xyz/api/v1/external")

# ─── Vòng Cấm TV config ──────────────────────────────────────────────────────
VONGCAM_FRONTEND_URL  = os.environ.get("VONGCAM_FRONTEND", "https://sv2.vongcam3.live/")
VONGCAM_API_URL       = os.environ.get("VONGCAM_API",      "https://sv.bugiotv.xyz/internal/api/matches")
VONGCAM_API_TOKEN     = os.environ.get("VONGCAM_TOKEN",    "AB321C")

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
_vongcam_api_cache  = {"url": VONGCAM_API_URL,        "discovered_at": 0}

# ─── Playlist content cache ───────────────────────────────────────────────────
def _empty_entry():
    return {"content": None, "gz": None, "etag": None, "built_at": 0,
            "lock": threading.Lock()}

_playlist_cache = {
    "combined": _empty_entry(),
    "tieulam":  _empty_entry(),
    "hoiquan":  _empty_entry(),
    "khandaia": _empty_entry(),
    "vongcam":  _empty_entry(),
    "vtv":      _empty_entry(),
}

_last_counts = {
    "tieulam": 0, "hoiquan": 0, "khandaia": 0, "vongcam": 0, "vtv": 0,
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


def _fetch_tieulam_from_cache() -> list:
    """Tải cache từ GitHub raw URL (cập nhật mỗi 30 phút bởi GitHub Actions).
    Raise ValueError nếu cache không có hoặc quá cũ.
    """
    if not TIEULAM_CACHE_URL:
        raise ValueError("TIEULAM_CACHE_URL not set")
    r = requests.get(TIEULAM_CACHE_URL, timeout=10)
    r.raise_for_status()
    payload = r.json()
    fetched_at = payload.get("fetched_at", 0)
    age = int(time.time()) - fetched_at
    if age > TIEULAM_CACHE_MAX_AGE:
        raise ValueError(f"Cache quá cũ: {age}s (max {TIEULAM_CACHE_MAX_AGE}s)")
    data = payload.get("data", [])
    if not data:
        raise ValueError("Cache rỗng")
    return data


def _call_one_relay(url: str) -> list:
    """Gọi một relay URL, raise ValueError nếu không thành công."""
    headers: dict = {}
    if TIEULAM_RELAY_SECRET:
        headers["X-Relay-Token"] = TIEULAM_RELAY_SECRET
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    rdata = resp.json()
    if "error" in rdata:
        raise ValueError(f"Relay error: {rdata['error']}")
    data = rdata.get("data", [])
    if not data:
        raise ValueError("Relay returned empty data")
    return data


def _fetch_tieulam_via_relay() -> list:
    """Thử relay URLs theo thứ tự: TIEULAM_RELAY_URL → TIEULAM_RELAY_URL_2.
    Raise ValueError nếu tất cả đều thất bại.
    """
    import sys
    last_err: Exception = ValueError("No relay URL configured")

    for url in filter(None, [TIEULAM_RELAY_URL, TIEULAM_RELAY_URL_2]):
        try:
            return _call_one_relay(url)
        except Exception as e:
            print(f"⚠️ Relay {url} failed: {e}", file=sys.stderr)
            last_err = e

    raise last_err


def _fetch_tieulam_matches() -> list:
    """Fetch TieuLam matches — 3-tier free setup:

    1. GitHub Actions cache (mỗi 30 phút, nhanh nhất, không gọi API)
    2. Relay (Cloudflare worker hoặc Replit relay — khi cache cũ/lỗi)
    3. Direct API (fallback cuối, chỉ OK khi không bị Cloudflare chặn)
    """
    import sys

    # Tầng 1 — GitHub Actions cache
    try:
        data = _fetch_tieulam_from_cache()
        print(f"✅ TieuLam cache: {len(data)} matches", file=sys.stderr)
        return data
    except Exception as e:
        print(f"⚠️ Cache miss: {e}", file=sys.stderr)

    # Tầng 2 — Relay (Cloudflare worker / Replit)
    if TIEULAM_RELAY_URL or TIEULAM_RELAY_URL_2:
        return _fetch_tieulam_via_relay()

    # Tầng 3 — Direct API (yêu cầu curl_cffi / cloudscraper)

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


_TIEULAM_SPORT_VI = {
    "FOOTBALL":    ("⚽ Bóng đá",   SPORT_LOGOS["football"]),
    "VOLLEYBALL":  ("🏐 Bóng chuyền", SPORT_LOGOS["volleyball"]),
    "BASKETBALL":  ("🏀 Bóng rổ",   SPORT_LOGOS["basketball"]),
    "TENNIS":      ("🎾 Quần vợt",  SPORT_LOGOS["tennis"]),
    "BADMINTON":   ("🏸 Cầu lông",  SPORT_LOGOS["badminton"]),
    "BILLIARD":    ("🎱 Bi-a",      SPORT_LOGOS["billiards"]),
    "SNOOKER":     ("🎱 Snooker",   SPORT_LOGOS["billiards"]),
}


def _tieulam_logo(match: dict) -> str:
    # Dùng icon môn thể thao (giống HQ/KDA) thay vì logo đội — nhất quán hơn
    desc = (match.get("desc") or "").upper()
    sport_info = _TIEULAM_SPORT_VI.get(desc)
    if sport_info:
        return sport_info[1]
    return _logo_from_text(desc + " " + match.get("league", ""))


def _tieulam_sport_label(match: dict) -> str:
    """Trả về nhãn môn thể thao tiếng Việt từ field desc."""
    desc = (match.get("desc") or "").upper()
    sport_info = _TIEULAM_SPORT_VI.get(desc)
    if sport_info:
        return sport_info[0]
    if desc:
        return desc.capitalize()
    return ""


def _build_tieulam_lines(matches: list) -> list:
    lines = []
    for match in matches:
        source_live = (match.get("source_live") or "").strip()
        blv         = (match.get("blv") or "").strip()
        stream_key  = (match.get("stream_key") or "").strip()

        if source_live:
            # Trận đang live — có URL CDN xác nhận
            stream_url = source_live
        elif blv and stream_key:
            # Trận có BLV được assign — dùng stream_key (BLV đã nhận kèo, sắp phát)
            stream_url = f"{TIEULAM_STREAM_CDN}/live/{stream_key}/playlist.m3u8"
        else:
            continue

        start_str = match.get("start_date", "")
        is_live = bool(match.get("is_live"))
        if start_str and not is_live:
            try:
                dt_start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if dt_start.tzinfo is None:
                    dt_start = dt_start.replace(tzinfo=timezone.utc)
                elapsed = time.time() - dt_start.timestamp()
                if blv:
                    # Trận BLV: cho phép tối đa 12h trước giờ đấu (hiện lịch World Cup ngày mai)
                    if elapsed < -259200:  # 72h trước (World Cup)
                        continue
                else:
                    # Trận ẩn danh: phải đã bắt đầu mới có stream
                    if elapsed < 0:
                        continue
                if elapsed > MATCH_MAX_AGE_SECONDS:
                    continue
            except Exception:
                pass

        logo  = _tieulam_logo(match)
        team1  = match.get("team_1", "Home").strip()
        team2  = match.get("team_2", "Away").strip()
        league = match.get("league", "").strip()
        blv    = (match.get("blv") or "").strip()
        sport  = _tieulam_sport_label(match)

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

        # Ưu tiên BLV nếu có, fallback hiển thị môn thể thao như HQ
        suffix = blv if blv else sport
        if suffix:
            display = f"{time_str} - {date_str} | {team1} VS {team2} ({league}) | {suffix}"
        else:
            display = f"{time_str} - {date_str} | {team1} VS {team2} ({league})"

        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="TieuLam TV",{display}')
        lines.append(stream_url)
    return lines



def _build_lines_from_fixtures(fixtures: list) -> list:
    """Chuyển fixtures (đã xử lý từ relay) thành M3U lines."""
    lines = []
    for f in fixtures:
        stream_url = (f.get("streamUrl") or "").strip()
        if not stream_url:
            continue
        logo  = f.get("logo") or f.get("sportLogo", "")
        group = f.get("groupTitle", "TieuLam TV")
        title = f.get("title", "")
        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="{group}",{title}')
        lines.append(stream_url)
    return lines


def _fetch_vtv_lines() -> list:
    """Fetch kênh VTV tĩnh từ GitHub M3U."""
    resp = requests.get(VTV_M3U_URL, timeout=10)
    resp.raise_for_status()
    result = []
    for line in resp.text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#EXTM3U"):
            continue
        result.append(stripped)
    return result


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
#  Vòng Cấm TV
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_vongcam_matches() -> list:
    """Fetch matches from Vòng Cấm TV API.

    Response: { code, message, data: [...] }
    Each item has commentator.streamSourceHd / .streamSourceSd for stream URLs.
    """
    headers = {
        "Authorization": f"Bearer {VONGCAM_API_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": VONGCAM_FRONTEND_URL,
        "Origin": VONGCAM_FRONTEND_URL.rstrip("/"),
    }
    try:
        resp = requests.get(VONGCAM_API_URL, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("data") or data.get("matches") or data.get("fixtures") or []
        return []
    except Exception as e:
        import sys
        print(f"⚠️ Vòng Cấm TV API error: {e}", file=sys.stderr)
        return []


def _build_vongcam_lines(matches: list) -> list:
    """Convert Vòng Cấm matches to M3U lines — format giống Hội Quán TV.

    API response shape (sv.bugiotv.xyz/internal/api/matches):
      - commentator.streamSourceHd / .streamSourceSd  — HLS URL (HD ưu tiên)
      - homeClub.name / awayClub.name                 — tên đội
      - tournamentName                                 — tên giải
      - isLive                                         — đang live
      - commentator.nickname                           — tên BLV
      - startTime                                      — giờ VN local (không có tz)
    """
    try:
        matches = sorted(matches, key=lambda m: m.get("startTime") or "")
    except Exception:
        pass

    lines = []
    now_ts = time.time()
    for match in matches:
        commentator = match.get("commentator") or {}

        # Chất lượng cao nhất: FHD → HD → SD
        stream_url = (
            commentator.get("streamSourceFhd") or
            commentator.get("streamSourceHd") or
            commentator.get("streamSourceSd") or
            ""
        ).strip()
        if not stream_url:
            continue

        # Lọc theo thời gian (bỏ qua trận đã kết thúc > MATCH_MAX_AGE_SECONDS)
        start_time_str = match.get("startTime", "")
        is_live = bool(match.get("isLive"))
        if start_time_str and not is_live:
            try:
                # startTime là giờ VN local ("2026-06-23T04:00:00"), không có tz
                dt = datetime.fromisoformat(start_time_str)
                dt = dt.replace(tzinfo=VN_TZ)
                elapsed = now_ts - dt.timestamp()
                if elapsed > MATCH_MAX_AGE_SECONDS:
                    continue
                # Chưa bắt đầu quá 72h: bỏ qua
                if elapsed < -259200:
                    continue
            except Exception:
                pass

        # Thông tin trận
        home_club = match.get("homeClub") or {}
        away_club = match.get("awayClub") or {}
        home   = home_club.get("name") or ""
        away   = away_club.get("name") or ""
        title  = match.get("title") or ""
        league = match.get("tournamentName") or ""
        blv    = (commentator.get("nickname") or commentator.get("fullName") or "").strip().upper()

        # Format giờ VN — giống Hội Quán TV
        if start_time_str:
            try:
                dt = datetime.fromisoformat(start_time_str)
                dt = dt.replace(tzinfo=VN_TZ)
                time_str = dt.strftime("%H:%M")
                date_str = dt.strftime("%d/%m")
            except Exception:
                time_str = "--:--"
                date_str = "--/--"
        else:
            time_str = "--:--"
            date_str = "--/--"

        # Tên kênh — đúng format Hội Quán: "HH:MM - DD/MM | Home VS Away (League) | BLV"
        if home and away:
            display = f"{time_str} - {date_str} | {home} VS {away} ({league})"
        elif title:
            display = f"{time_str} - {date_str} | {title}"
        else:
            continue

        if blv:
            display += f" | {blv}"

        # Logo: sport emoji dựa trên tên giải (giống Hội Quán — nhất quán hơn team logo)
        logo = _logo_from_text(f"{home} {away} {league}")

        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="Vòng Cấm TV",{display}')
        lines.append(stream_url)

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
        # _fetch_tieulam_matches() xử lý relay/direct logic — raise nếu lỗi
        # Exception sẽ bị bắt bởi ThreadPoolExecutor và ghi vào errors list
        return _build_tieulam_lines(_fetch_tieulam_matches())

    def fetch_hq():
        return _build_fixture_lines(_fetch_hoiquan_fixtures(), "Hội Quán TV")

    def fetch_kda():
        return _build_fixture_lines(_fetch_khandaia_fixtures(), "Khán Đài A")

    def fetch_vongcam():
        return _build_vongcam_lines(_fetch_vongcam_matches())

    def fetch_vtv():
        try:
            return _fetch_vtv_lines()
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {
            ex.submit(fetch_tieulam): "tieulam",
            ex.submit(fetch_hq):      "hoiquan",
            ex.submit(fetch_kda):     "khandaia",
            ex.submit(fetch_vongcam): "vongcam",
            ex.submit(fetch_vtv):     "vtv",
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
    vongcam_lines = results.get("vongcam",  [])
    vtv_lines     = results.get("vtv",      [])

    err_str = "; ".join(errors)

    def count(lines):
        return sum(1 for l in lines if l.startswith("#EXTINF"))

    current_epg = _epg_url()
    epg_header = f'#EXTM3U url-tvg="{current_epg}" x-tvg-url="{current_epg}"'

    _store("tieulam",  epg_header + "\n" + "\n".join(tieulam_lines))
    _store("hoiquan",  epg_header + "\n" + "\n".join(hq_lines))
    _store("khandaia", epg_header + "\n" + "\n".join(kda_lines))
    _store("vongcam",  epg_header + "\n" + "\n".join(vongcam_lines))
    _store("vtv",      epg_header + "\n" + "\n".join(vtv_lines))

    all_lines = tieulam_lines + hq_lines + kda_lines + vongcam_lines + vtv_lines
    combined_text = epg_header + "\n" + "\n".join(all_lines)
    if err_str:
        combined_text += f"\n# Errors: {err_str}"
    _store("combined", combined_text)

    _last_counts.update({
        "tieulam":      count(tieulam_lines),
        "hoiquan":      count(hq_lines),
        "khandaia":     count(kda_lines),
        "vongcam":      count(vongcam_lines),
        "vtv":          count(vtv_lines),
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


@app.route("/vongcam.m3u")
def vongcam_m3u():
    return _m3u_response("vongcam", "vongcam.m3u")


@app.route("/vtv.m3u")
def vtv_m3u():
    return _m3u_response("vtv", "vtv.m3u")


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


@app.route("/api/debug")
def debug_status():
    """Kiểm tra live trạng thái từng nguồn — hữu ích khi debug trên Vercel."""
    import sys
    result = {
        "config": {
            "tieulam_cache_url":    TIEULAM_CACHE_URL or None,
            "tieulam_relay_url":    TIEULAM_RELAY_URL or None,
            "tieulam_relay_url_2":  TIEULAM_RELAY_URL_2 or None,
            "tieulam_relay_secret": "SET" if TIEULAM_RELAY_SECRET else "NOT SET",
            "tieulam_api_cache":    _tieulam_api_cache["url"],
            "tieulam_frontend":     TIEULAM_FRONTEND_URL,
        },
        "cached_counts": {
            "tieulam":  _last_counts.get("tieulam", 0),
            "hoiquan":  _last_counts.get("hoiquan", 0),
            "khandaia": _last_counts.get("khandaia", 0),
            "vongcam":  _last_counts.get("vongcam", 0),
            "vtv":      _last_counts.get("vtv", 0),
            "last_error": _last_counts.get("last_error", ""),
        },
        "live_tests": {}
    }

    # Test GitHub Actions cache
    try:
        cache_data = _fetch_tieulam_from_cache()
        result["live_tests"]["gh_cache"] = {
            "ok": True, "count": len(cache_data), "url": TIEULAM_CACHE_URL,
        }
    except Exception as e:
        result["live_tests"]["gh_cache"] = {
            "ok": False, "error": str(e), "url": TIEULAM_CACHE_URL,
        }

    # Test từng relay riêng
    for label, url in [("relay_1", TIEULAM_RELAY_URL), ("relay_2", TIEULAM_RELAY_URL_2)]:
        if not url:
            result["live_tests"][label] = {"ok": None, "url": None, "msg": "not configured"}
            continue
        try:
            data = _call_one_relay(url)
            result["live_tests"][label] = {"ok": True, "count": len(data), "url": url}
        except Exception as e:
            result["live_tests"][label] = {"ok": False, "error": str(e), "url": url}

    # Test direct API (timeout ngắn để debug nhanh)
    try:
        hdrs = {**_TIEULAM_HTTPX_HEADERS, "User-Agent": "Mozilla/5.0"}
        payload = {
            "queries": [{"field": "start_date", "type": "gte", "value": (datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S")}],
            "query_and": True, "limit": 3, "page": 1,
        }
        r = requests.post(
            TIEULAM_KNOWN_API_BASE + "/matches/graph",
            json=payload, headers=hdrs, timeout=8,
        )
        result["live_tests"]["direct_api"] = {
            "ok": r.ok, "status": r.status_code,
            "url": TIEULAM_KNOWN_API_BASE + "/matches/graph",
            "count": len((r.json().get("data") or [])) if r.ok else 0,
        }
    except Exception as e:
        result["live_tests"]["direct_api"] = {
            "ok": False, "error": str(e),
            "url": TIEULAM_KNOWN_API_BASE + "/matches/graph",
        }

    return result


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
    vongcam_count = _last_counts.get("vongcam", 0)
    vtv_count     = _last_counts.get("vtv", 0)
    total         = tieulam_count + hq_count + kda_count + vongcam_count + vtv_count

    epg_link = _epg_url()
    return (
        "<h2>🎬 IPTV M3U Server</h2>"
        "<h3>📋 Playlist</h3><ul>"
        "<li><a href='/live.m3u'>/live.m3u</a> — Tất cả nguồn gộp lại</li>"
        "<li><a href='/tieulam.m3u'>/tieulam.m3u</a> — TieuLam TV only</li>"
        "<li><a href='/hoiquan.m3u'>/hoiquan.m3u</a> — Hội Quán TV only</li>"
        "<li><a href='/khandaia.m3u'>/khandaia.m3u</a> — Khán Đài A only</li>"
        "<li><a href='/vongcam.m3u'>/vongcam.m3u</a> — Vòng Cấm TV only</li>"
        "<li><a href='/vtv.m3u'>/vtv.m3u</a> — Kênh VTV tĩnh (VTV1-10, Vietnam Today)</li>"
        "</ul>"
        "<h3>📡 EPG</h3><ul>"
        f"<li><a href='/epg.xml'>/epg.xml</a> — XMLTV tự sinh từ danh sách kênh (cache 1h)</li>"
        f"<li>URL đầy đủ: <code>{epg_link}</code></li>"
        "</ul>"
        "<h3>📊 Trạng thái</h3>"
        f"<p>📺 Tổng kênh live: <strong>{total}</strong></p>"
        f"<p>🕐 Cập nhật lần cuối: <strong>{dt_str}</strong></p>"
        f"<p>⏳ Cập nhật tiếp theo: <strong>{next_str}</strong></p>"
        f"<p>🟢 TieuLam TV: <strong>{tieulam_count} kênh</strong>"
        f"&nbsp;|&nbsp; <code>{_tieulam_api_cache['url']}</code></p>"
        f"<p>🟢 Hội Quán TV: <strong>{hq_count} kênh</strong>"
        f"&nbsp;|&nbsp; <code>{_hoiquan_api_cache['url']}</code></p>"
        f"<p>🟢 Khán Đài A: <strong>{kda_count} kênh</strong>"
        f"&nbsp;|&nbsp; <code>{_khandaia_api_cache['url']}</code></p>"
        f"<p>🟢 Vòng Cấm TV: <strong>{vongcam_count} kênh</strong>"
        f"&nbsp;|&nbsp; <code>{VONGCAM_API_URL}</code></p>"
        f"<p>📡 VTV (tĩnh): <strong>{vtv_count} kênh</strong></p>"
        f"{err_html}"
        "<h3>⚙️ Tối ưu băng thông</h3>"
        "<ul>"
        "<li>Gzip nén tự động (giảm ~70% dữ liệu truyền)</li>"
        "<li>ETag + HTTP 304 — client có sẵn cache không cần tải lại</li>"
        f"<li>Cache-Control: public, max-age={PREFETCH_INTERVAL}s</li>"
        "<li>Fetch 5 nguồn song song (ThreadPoolExecutor)</li>"
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

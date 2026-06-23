"""Fetch TieuLam matches and write to data/tieulam_cache.json.
Chạy bởi GitHub Actions mỗi 30 phút — tạo cache cho main.py trên Vercel đọc.

Thứ tự ưu tiên:
  1. Direct API (GitHub Actions IPs thường không bị Cloudflare chặn)
  2. Relay (Cloudflare worker hoặc Replit relay nếu set TIEULAM_RELAY_URL)
"""
import json, os, sys, time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

TIEULAM_API = "https://api.tlap17062026.com/matches/graph"
FRONTEND    = "https://sv2.tieulam.info"

HEADERS = {
    "Content-Type":    "application/json",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "vi-VN,vi;q=0.9",
    "Origin":          FRONTEND,
    "Referer":         FRONTEND + "/",
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "sec-fetch-dest":  "empty",
    "sec-fetch-mode":  "cors",
    "sec-fetch-site":  "cross-site",
}


def fetch_direct() -> list:
    now = datetime.now(timezone.utc)
    cutoff     = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_end = (now + timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%S")
    payload = {
        "queries": [
            {"field": "start_date", "type": "gte", "value": cutoff},
            {"field": "start_date", "type": "lte", "value": cutoff_end},
        ],
        "query_and": True,
        "limit": 100, "page": 1,
        "order_asc": "start_date",
    }
    r = requests.post(TIEULAM_API, json=payload, headers=HEADERS, timeout=15)
    r.raise_for_status()
    if r.status_code == 403:
        raise ValueError("direct: 403 Cloudflare blocked")
    data = r.json().get("data", [])
    if not data:
        raise ValueError("direct: empty response")
    return data


def fetch_relay(url: str, secret: str = "") -> list:
    hdrs: dict = {}
    if secret:
        hdrs["X-Relay-Token"] = secret
    r = requests.get(url, headers=hdrs, timeout=15)
    r.raise_for_status()
    rdata = r.json()
    if "error" in rdata:
        raise ValueError(f"relay error: {rdata['error']}")
    data = rdata.get("data", [])
    if not data:
        raise ValueError("relay: empty response")
    return data


RELAY_URL    = os.environ.get("TIEULAM_RELAY_URL", "")
RELAY_SECRET = os.environ.get("RELAY_SECRET", "")

data   = None
source = None

# 1. Direct API
try:
    data   = fetch_direct()
    source = "direct"
    print(f"✅ Direct API: {len(data)} matches")
except Exception as e:
    print(f"⚠️  Direct failed: {e}", file=sys.stderr)

# 2. Relay
if data is None and RELAY_URL:
    try:
        data   = fetch_relay(RELAY_URL, RELAY_SECRET)
        source = "relay"
        print(f"✅ Relay ({RELAY_URL}): {len(data)} matches")
    except Exception as e:
        print(f"⚠️  Relay failed: {e}", file=sys.stderr)

if data is None:
    print("❌ All sources failed — keeping existing cache", file=sys.stderr)
    sys.exit(0)

out = Path("data/tieulam_cache.json")
out.parent.mkdir(exist_ok=True)
out.write_text(
    json.dumps({
        "fetched_at":     int(time.time()),
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "source":         source,
        "count":          len(data),
        "data":           data,
    }, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
print(f"✅ Saved {len(data)} matches → {out}  (source: {source})")

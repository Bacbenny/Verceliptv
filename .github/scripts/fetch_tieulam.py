"""Fetch TieuLam match data -> data/tieulam_cache.json
GitHub Actions script: GH IPs không bị Cloudflare chặn -> gọi API trực tiếp.
"""
import json, os, re, sys, time
import requests
from datetime import datetime, timezone, timedelta

FRONTEND_URL = os.environ.get("TIEULAM_FRONTEND", "https://sv2.tieulam.info")
API_BASE_ENV = os.environ.get("TIEULAM_API_BASE", "")
MATCH_MAX_AGE = 7200
LOOKAHEAD_H   = 72

HEADERS = {
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.7",
    "Content-Type":    "application/json",
    "Referer":         FRONTEND_URL + "/",
    "Origin":          FRONTEND_URL,
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}


def discover_api() -> str:
    try:
        r = requests.get(FRONTEND_URL, timeout=15, headers={"User-Agent": HEADERS["User-Agent"]})
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        for js_path in js_files[:3]:
            js = requests.get(
                FRONTEND_URL.rstrip("/") + js_path, timeout=20,
                headers={"User-Agent": HEADERS["User-Agent"]}).text
            for pat in [
                r'create\(\{baseURL:"(https://[^"]+)"\}',
                r'baseURL:"(https://[^"]{10,60})"',
            ]:
                hits = re.findall(pat, js)
                if hits:
                    url = hits[0].rstrip("/")
                    print(f"Discovered API: {url}", file=sys.stderr)
                    return url
    except Exception as e:
        print(f"Discovery error: {e}", file=sys.stderr)
    return ""


def fetch_matches(api_base: str) -> list:
    now = datetime.now(timezone.utc)
    cutoff     = (now - timedelta(seconds=MATCH_MAX_AGE)).strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_end = (now + timedelta(hours=LOOKAHEAD_H)).strftime("%Y-%m-%dT%H:%M:%S")
    payload = {
        "queries": [
            {"field": "start_date", "type": "gte", "value": cutoff},
            {"field": "start_date", "type": "lte", "value": cutoff_end},
        ],
        "query_and": True, "limit": 100, "page": 1, "order_asc": "start_date",
    }
    url = api_base.rstrip("/") + "/matches/graph"
    print(f"POST {url}", file=sys.stderr)
    resp = requests.post(url, json=payload, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json().get("data", [])


def main():
    candidates = []
    if API_BASE_ENV:
        candidates.append(API_BASE_ENV)
    discovered = discover_api()
    if discovered and discovered not in candidates:
        candidates.insert(0, discovered)
    for fallback in ["https://api.tlap17062026.com", "https://api.tlap12062026.xyz"]:
        if fallback not in candidates:
            candidates.append(fallback)

    data = []
    for api_base in candidates:
        for attempt in range(2):
            try:
                data = fetch_matches(api_base)
                if data:
                    print(f"OK: {len(data)} matches from {api_base}", file=sys.stderr)
                    break
            except Exception as e:
                print(f"  Attempt {attempt+1} failed ({api_base}): {e}", file=sys.stderr)
                time.sleep(3)
        if data:
            break

    if not data:
        print("All API attempts failed — keeping existing cache", file=sys.stderr)
        sys.exit(0)

    cache = {
        "fetched_at":     int(time.time()),
        "fetched_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source":         "github-actions",
        "count":          len(data),
        "data":           data,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/tieulam_cache.json", "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Saved {len(data)} matches to data/tieulam_cache.json")


if __name__ == "__main__":
    main()

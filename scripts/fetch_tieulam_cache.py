"""Fetch TieuLam TV channels from tinhlagi.pro's public M3U list -> data/tieulam_cache.json
Chạy bởi GitHub Actions mỗi 30 phút — tạo cache cho main.py trên Vercel đọc.

Nguồn dữ liệu: https://tinhlagi.pro/s.m3u — chỉ lấy nhóm 'TIẾU LÂM TV' (group-title).
Đây là danh sách kênh đã build sẵn (không phải API trận đấu riêng của TieuLam),
nên không cần relay/bypass Cloudflare.
"""
import json, os, re, sys, time
import requests
from datetime import datetime, timezone
from pathlib import Path

TINHLAGI_M3U_URL = os.environ.get("TINHLAGI_M3U_URL", "https://tinhlagi.pro/s.m3u")
GROUP_MATCH = "TIẾU LÂM"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
}


def parse_tieulam(text: str) -> list:
    lines = text.splitlines()
    channels = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF"):
            m = re.search(r'group-title="([^"]*)"', line)
            group = m.group(1) if m else ""
            if GROUP_MATCH in group.upper():
                logo_m = re.search(r'tvg-logo="([^"]*)"', line)
                logo = logo_m.group(1) if logo_m else ""
                title = line.split(",", 1)[1].strip() if "," in line else ""
                referrer = ""
                url = ""
                j = i + 1
                while j < len(lines) and not lines[j].startswith("#EXTINF") and lines[j].strip():
                    l2 = lines[j]
                    if l2.startswith("#EXTVLCOPT:http-referrer="):
                        referrer = l2.split("=", 1)[1].strip()
                    elif not l2.startswith("#"):
                        url = l2.strip()
                    j += 1
                if url:
                    title_upper = title.upper()
                    if "(HD2)" in title_upper or "NHÀ ĐÀI" in title_upper:
                        i = j
                        continue
                    channels.append({
                        "title": title,
                        "logo": logo,
                        "referrer": referrer,
                        "url": url,
                    })
                i = j
                continue
        i += 1
    return channels


def main():
    r = requests.get(TINHLAGI_M3U_URL, headers=HEADERS, timeout=20)
    r.raise_for_status()
    channels = parse_tieulam(r.text)
    if not channels:
        print("❌ Không tìm thấy kênh Tiếu Lâm TV — giữ nguyên cache cũ", file=sys.stderr)
        sys.exit(0)

    out = Path("data/tieulam_cache.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps({
            "source":         "tinhlagi",
            "source_url":     TINHLAGI_M3U_URL,
            "fetched_at":     int(time.time()),
            "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
            "count":          len(channels),
            "channels":       channels,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"✅ Saved {len(channels)} channels (Tiếu Lâm TV) → {out}")


if __name__ == "__main__":
    main()

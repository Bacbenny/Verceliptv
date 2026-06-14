import { Router, type IRouter } from "express";

const router: IRouter = Router();

// ── Config ────────────────────────────────────────────────────────────────────
const TIEULAM_FRONTEND_URL =
  process.env["TIEULAM_FRONTEND"] ?? "https://sv1.tieulam1.live";
const TIEULAM_KNOWN_API_BASE =
  process.env["TIEULAM_API"] ?? "https://api.tlap12062026.xyz";
const TIEULAM_STREAM_CDN =
  process.env["TIEULAM_CDN"] ?? "https://live.secufun.xyz";
const RELAY_SECRET = process.env["RELAY_SECRET"] ?? "";
const MATCH_MAX_AGE_SECONDS = parseInt(
  process.env["MATCH_MAX_DURATION"] ?? "7200",
  10,
);

// ── Sport labels & logos ──────────────────────────────────────────────────────
const CDN =
  "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72";
const SPORT_MAP: Record<string, { label: string; logo: string }> = {
  FOOTBALL:   { label: "⚽ Bóng đá",     logo: `${CDN}/26bd.png` },
  VOLLEYBALL: { label: "🏐 Bóng chuyền", logo: `${CDN}/1f3d0.png` },
  BASKETBALL: { label: "🏀 Bóng rổ",     logo: `${CDN}/1f3c0.png` },
  TENNIS:     { label: "🎾 Quần vợt",    logo: `${CDN}/1f3be.png` },
  BADMINTON:  { label: "🏸 Cầu lông",    logo: `${CDN}/1f3f8.png` },
  BILLIARD:   { label: "🎱 Bi-a",        logo: `${CDN}/1f3b1.png` },
  SNOOKER:    { label: "🎱 Snooker",     logo: `${CDN}/1f3b1.png` },
};
const DEFAULT_LOGO = `${CDN}/1f3c6.png`;

function sportInfo(desc: string): { label: string; logo: string } {
  return SPORT_MAP[desc.toUpperCase()] ?? { label: desc || "Thể thao", logo: DEFAULT_LOGO };
}

// ── VN timezone helper ────────────────────────────────────────────────────────
const VN_OFFSET_MS = 7 * 60 * 60 * 1000;

function toVnTime(isoStr: string): { time: string; date: string; iso: string } {
  try {
    const utc = new Date(isoStr).getTime();
    const vn  = new Date(utc + VN_OFFSET_MS);
    const hh  = String(vn.getUTCHours()).padStart(2, "0");
    const mm  = String(vn.getUTCMinutes()).padStart(2, "0");
    const dd  = String(vn.getUTCDate()).padStart(2, "0");
    const mo  = String(vn.getUTCMonth() + 1).padStart(2, "0");
    return { time: `${hh}:${mm}`, date: `${dd}/${mo}`, iso: vn.toISOString() };
  } catch {
    return { time: "--:--", date: "--/--", iso: isoStr };
  }
}

// ── API URL discovery ─────────────────────────────────────────────────────────
const FETCH_HEADERS: Record<string, string> = {
  Accept: "application/json, text/plain, */*",
  "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
  "Content-Type": "application/json",
  Referer: TIEULAM_FRONTEND_URL + "/",
  Origin: TIEULAM_FRONTEND_URL,
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
  "sec-fetch-dest": "empty",
  "sec-fetch-mode": "cors",
  "sec-fetch-site": "cross-site",
};

let _apiUrl = TIEULAM_KNOWN_API_BASE + "/matches/graph";
let _apiDiscoveredAt = 0;
const API_DISCOVERY_TTL_MS = 3600 * 1000;

async function discoverApiUrl(): Promise<string> {
  try {
    const res = await fetch(TIEULAM_FRONTEND_URL, {
      headers: { "User-Agent": FETCH_HEADERS["User-Agent"]! },
      signal: AbortSignal.timeout(10_000),
    });
    const html = await res.text();
    const jsPaths = [...html.matchAll(/src="(\/assets\/[^"]+\.js)"/g)].map(
      (m) => m[1],
    );
    for (const jsPath of jsPaths.slice(0, 3)) {
      const js = await fetch(
        TIEULAM_FRONTEND_URL.replace(/\/$/, "") + jsPath,
        { headers: { "User-Agent": FETCH_HEADERS["User-Agent"]! }, signal: AbortSignal.timeout(15_000) },
      ).then((r) => r.text());
      const hit =
        js.match(/create\(\{baseURL:"(https:\/\/[^"]+)"\}/)?.at(1) ??
        js.match(/baseURL:"(https:\/\/[^"]{10,60})"/)?.at(1);
      if (hit) return hit.replace(/\/$/, "") + "/matches/graph";
    }
  } catch { /* fallback */ }
  return TIEULAM_KNOWN_API_BASE + "/matches/graph";
}

async function getApiUrl(): Promise<string> {
  const now = Date.now();
  if (now - _apiDiscoveredAt > API_DISCOVERY_TTL_MS) {
    _apiUrl = await discoverApiUrl();
    _apiDiscoveredAt = now;
  }
  return _apiUrl;
}

// ── Raw fetch from TieuLam API ────────────────────────────────────────────────
interface RawMatch {
  id?: string;
  team_1?: string;
  team_2?: string;
  team_1_logo?: string;
  team_2_logo?: string;
  team_1_score?: number;
  team_2_score?: number;
  league?: string;
  desc?: string;
  blv?: string;
  stream_key?: string;
  source_live?: string;
  is_live?: boolean;
  is_hot?: boolean;
  start_date?: string;
}

async function fetchRawMatches(): Promise<RawMatch[]> {
  const cutoffMs    = Date.now() - MATCH_MAX_AGE_SECONDS * 1000;
  const cutoffEndMs = Date.now() + 72 * 3600 * 1000;
  const toIso = (ms: number) => new Date(ms).toISOString().replace(/\.\d+Z$/, "");

  const payload = {
    queries: [
      { field: "start_date", type: "gte", value: toIso(cutoffMs) },
      { field: "start_date", type: "lte", value: toIso(cutoffEndMs) },
    ],
    query_and: true,
    limit: 100,
    page: 1,
    order_asc: "start_date",
  };

  const tryFetch = async (url: string): Promise<RawMatch[]> => {
    const res = await fetch(url, {
      method: "POST",
      headers: FETCH_HEADERS,
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(15_000),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const json = (await res.json()) as { data?: RawMatch[] };
    return json.data ?? [];
  };

  let apiUrl = await getApiUrl();
  try {
    return await tryFetch(apiUrl);
  } catch {
    _apiDiscoveredAt = 0;
    apiUrl = await getApiUrl();
    return await tryFetch(apiUrl);
  }
}

// ── Build enriched fixture (Hội Quán-like format) ─────────────────────────────
interface Fixture {
  id: string;
  title: string;
  team1: string;
  team2: string;
  team1Logo: string;
  team2Logo: string;
  score: string;
  league: string;
  sport: string;
  sportLogo: string;
  logo: string;         // team1Logo nếu có BLV, ngược lại sportLogo
  blv: string;
  streamUrl: string;
  isLive: boolean;
  isUpcoming: boolean;
  isHot: boolean;
  startTimeVN: string;
  startDateVN: string;
  startTimeIso: string;
  groupTitle: string;
}

function buildFixtures(matches: RawMatch[]): Fixture[] {
  const now = Date.now();
  const fixtures: Fixture[] = [];

  for (const m of matches) {
    const sourceLive = (m.source_live ?? "").trim();
    const blv        = (m.blv ?? "").trim();
    const streamKey  = (m.stream_key ?? "").trim();

    // Chỉ lấy trận có stream URL
    let streamUrl = "";
    if (sourceLive) {
      streamUrl = sourceLive;
    } else if (blv && streamKey) {
      streamUrl = `${TIEULAM_STREAM_CDN}/live/${streamKey}/playlist.m3u8`;
    } else {
      continue;
    }

    // Lọc theo thời gian
    const startStr  = m.start_date ?? "";
    const isLive    = Boolean(m.is_live);
    let   isUpcoming = false;
    if (startStr && !isLive) {
      const startMs = new Date(startStr).getTime();
      if (isNaN(startMs)) continue;
      const elapsed = now - startMs;
      if (blv) {
        // BLV: hiện tối đa 72h trước giờ đấu (World Cup lịch xa)
        if (elapsed < -72 * 3600 * 1000) continue;
        if (elapsed < 0) isUpcoming = true;
      } else {
        if (elapsed < 0) continue;   // Ẩn danh: phải đã bắt đầu
      }
      if (elapsed > MATCH_MAX_AGE_SECONDS * 1000) continue;
    }

    const desc       = (m.desc ?? "").toUpperCase();
    const sport      = sportInfo(desc);
    const team1      = (m.team_1 ?? "Home").trim();
    const team2      = (m.team_2 ?? "Away").trim();
    const team1Logo  = (m.team_1_logo ?? "").trim();
    const team2Logo  = (m.team_2_logo ?? "").trim();
    const league     = (m.league ?? "").trim();
    const vn         = toVnTime(startStr);
    const suffix     = blv || sport.label;
    const score      = isLive
      ? `${m.team_1_score ?? 0} - ${m.team_2_score ?? 0}`
      : "";

    // Logo: dùng logo đội nhà nếu có BLV (trận sắp tới), ngược lại dùng sport logo
    const logo = blv && team1Logo ? team1Logo : sport.logo;

    const statusTag = isUpcoming ? "[Sắp diễn ra] " : "";
    const title = suffix
      ? `${statusTag}${vn.time} - ${vn.date} | ${team1} VS ${team2} (${league}) | ${suffix}`
      : `${statusTag}${vn.time} - ${vn.date} | ${team1} VS ${team2} (${league})`;

    fixtures.push({
      id:           m.id ?? streamKey,
      title,
      team1,
      team2,
      team1Logo,
      team2Logo,
      score,
      league,
      sport:        sport.label,
      sportLogo:    sport.logo,
      logo,
      blv,
      streamUrl,
      isLive,
      isUpcoming,
      isHot:        Boolean(m.is_hot),
      startTimeVN:  vn.time,
      startDateVN:  vn.date,
      startTimeIso: vn.iso,
      groupTitle:   "TieuLam TV",
    });
  }

  return fixtures;
}

// ── Route ─────────────────────────────────────────────────────────────────────
router.get("/tieulam-relay-public", async (req, res) => {
  if (RELAY_SECRET) {
    const token = req.headers["x-relay-token"];
    if (token !== RELAY_SECRET) {
      res.status(403).json({ error: "Forbidden" });
      return;
    }
  }

  try {
    const rawMatches = await fetchRawMatches();
    const fixtures   = buildFixtures(rawMatches);

    const liveCount = fixtures.filter((f) => f.isLive).length;
    const blvCount  = fixtures.filter((f) => f.blv).length;

    res.json({
      // Tương thích ngược với main.py (đọc data[])
      data: rawMatches,

      // Danh sách đã xử lý — format giống Hội Quán
      fixtures,

      // Metadata tổng hợp
      meta: {
        domain:        TIEULAM_FRONTEND_URL,
        api:           _apiUrl,
        streamCdn:     TIEULAM_STREAM_CDN,
        totalRaw:      rawMatches.length,
        totalFixtures: fixtures.length,
        liveCount,
        blvCount,
        fetchedAt:     new Date().toISOString(),
      },
    });
  } catch (err) {
    req.log.error({ err }, "tieulam-relay fetch failed");
    res.status(502).json({ error: "Upstream fetch failed" });
  }
});

export default router;

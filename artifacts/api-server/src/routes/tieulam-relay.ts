import { Router, type Request, type Response, type IRouter } from "express";

const router: IRouter = Router();

const TIEULAM_FRONTEND   = process.env.TIEULAM_FRONTEND   ?? "https://sv1.tieulam1.live";
const TIEULAM_API        = process.env.TIEULAM_API         ?? "https://api.tlap12062026.xyz";
const RELAY_SECRET       = process.env.RELAY_SECRET        ?? "";
const MATCH_MAX_DURATION = parseInt(process.env.MATCH_MAX_DURATION ?? "7200", 10);

// TieuLam stores all dates in Vietnam time (UTC+7) without timezone suffix.
// Queries must be sent as VN time strings; comparisons must parse as VN time.
const VN_OFFSET_MS = 7 * 60 * 60 * 1000;
const toVNDateStr = (ms: number): string =>
  new Date(ms + VN_OFFSET_MS).toISOString().slice(0, 19); // e.g. "2026-06-14T09:45:00"

/** Parse a VN-time ISO string (no tz suffix) → UTC milliseconds */
const vnStrToMs = (s: string): number =>
  new Date(s.includes("Z") || s.includes("+") ? s : s + "+07:00").getTime();

/** Discover the current TieuLam API base URL from any of the known frontends */
async function discoverApiBase(): Promise<string> {
  const frontends = [
    TIEULAM_FRONTEND,
    "https://sv2.tieulam1.live",
    "https://sv1.tieulam2.live",
    "https://sv2.tieulam2.live",
  ];
  const ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";

  for (const fe of frontends) {
    try {
      const html = await fetch(fe, { headers: { "User-Agent": ua }, signal: AbortSignal.timeout(8000) }).then(r => r.text());
      const jsMatch = html.match(/src="(\/assets\/[^"]+\.js)"/);
      if (!jsMatch) continue;
      const js = await fetch(fe.replace(/\/$/, "") + jsMatch[1], { headers: { "User-Agent": ua }, signal: AbortSignal.timeout(15000) }).then(r => r.text());
      const hit = js.match(/baseURL:"(https:\/\/[^"]{8,80})"/);
      if (hit) return hit[1].replace(/\/$/, "");
    } catch {
      // try next
    }
  }
  return TIEULAM_API.replace(/\/$/, "");
}

let _cachedApiBase: string = TIEULAM_API.replace(/\/$/, "");
let _cacheTs = 0;
const API_CACHE_TTL_MS = 3600_000; // 1 hour

async function getApiBase(): Promise<string> {
  if (Date.now() - _cacheTs > API_CACHE_TTL_MS) {
    _cachedApiBase = await discoverApiBase();
    _cacheTs = Date.now();
  }
  return _cachedApiBase;
}

/** Core logic — fetch, deduplicate and sort TieuLam matches */
async function fetchTieuLamData(req: Request, res: Response): Promise<void> {
  const now = Date.now();

  // All cutoff strings must be in Vietnam time so TieuLam backend interprets them correctly
  // Look back 6h so matches that started before MATCH_MAX_DURATION are still captured
  const LOOKBACK_MS = Math.max(MATCH_MAX_DURATION * 1000, 6 * 3600 * 1000);
  const cutoff    = toVNDateStr(now - LOOKBACK_MS);
  const cutoffEnd = toVNDateStr(now + 24 * 3600 * 1000);

  const apiBase = await getApiBase();
  const apiUrl  = `${apiBase}/matches/graph`;

  const headers: Record<string, string> = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Content-Type": "application/json",
    "Referer": `${TIEULAM_FRONTEND}/`,
    "Origin": TIEULAM_FRONTEND,
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "cross-site",
    "User-Agent":
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  };

  const timeWindowPayload = (page: number) => ({
    queries: [
      { field: "start_date", type: "gte", value: cutoff },
      { field: "start_date", type: "lte", value: cutoffEnd },
    ],
    query_and: true,
    limit: 100,
    page,
    order_asc: "start_date",
  });

  // Fetch BLV matches up to 72h ahead (VN time) — wide window to catch all upcoming commentated matches
  const blvCutoff    = toVNDateStr(now - MATCH_MAX_DURATION * 1000);
  const blvCutoffEnd = toVNDateStr(now + 72 * 3600 * 1000);
  const blvPayload = {
    queries: [
      { field: "blv",        type: "is_not_null", value: "" },
      { field: "start_date", type: "gte",          value: blvCutoff },
      { field: "start_date", type: "lte",          value: blvCutoffEnd },
    ],
    query_and: true,
    limit: 100,
    page: 1,
  };

  try {
    const [r1, r2, rBlv] = await Promise.all([
      fetch(apiUrl, { method: "POST", headers, body: JSON.stringify(timeWindowPayload(1)) }),
      fetch(apiUrl, { method: "POST", headers, body: JSON.stringify(timeWindowPayload(2)) }),
      fetch(apiUrl, { method: "POST", headers, body: JSON.stringify(blvPayload) }),
    ]);

    if (!r1.ok) {
      // Invalidate cache so next request re-discovers the API URL
      _cacheTs = 0;
      req.log.warn({ status: r1.status, apiUrl }, "TieuLam upstream error");
      res.status(502).json({ error: `Upstream ${r1.status}`, data: [] });
      return;
    }

    const j1   = (await r1.json()) as { data?: unknown[] };
    const j2   = r2.ok   ? ((await r2.json())   as { data?: unknown[] }) : { data: [] };
    const jBlv = rBlv.ok ? ((await rBlv.json()) as { data?: unknown[] }) : { data: [] };

    // Filter BLV matches: must have blv set and be within active window (VN time aware)
    const cutoffMs    = now - MATCH_MAX_DURATION * 1000;
    const blvWindowMs = now + 48 * 3600 * 1000;
    const blvMatches = (jBlv.data ?? []).filter(m => {
      const match = m as Record<string, unknown>;
      if (!match.blv) return false;
      const sd = String(match.start_date ?? "");
      if (!sd) return true;
      const t = vnStrToMs(sd);
      return t >= cutoffMs && t <= blvWindowMs;
    });

    const seen = new Set<string>();
    const combined: unknown[] = [];
    for (const m of [...blvMatches, ...(j1.data ?? []), ...(j2.data ?? [])]) {
      const match = m as Record<string, unknown>;
      const key = String(match.id ?? match.stream_key ?? JSON.stringify(m));
      if (!seen.has(key)) { seen.add(key); combined.push(m); }
    }

    // Sort by start_date ascending (VN time strings sort correctly as-is)
    combined.sort((a, b) => {
      const ta = String((a as Record<string, unknown>).start_date ?? "");
      const tb = String((b as Record<string, unknown>).start_date ?? "");
      return ta.localeCompare(tb);
    });

    res.json({ data: combined });
  } catch (err) {
    _cacheTs = 0; // force re-discover on next request
    req.log.error({ err }, "TieuLam relay fetch failed");
    res.status(502).json({ error: String(err), data: [] });
  }
}

/**
 * /api/tieulam-relay  — secured with RELAY_SECRET header
 */
router.get("/tieulam-relay", async (req, res) => {
  if (RELAY_SECRET) {
    const token = req.headers["x-relay-token"];
    if (token !== RELAY_SECRET) {
      res.status(401).json({ error: "Unauthorized" });
      return;
    }
  }
  await fetchTieuLamData(req, res);
});

/**
 * /api/tieulam-relay-public  — no auth required
 * Auto-used by Vercel as fallback when TIEULAM_RELAY_URL is not set.
 */
router.get("/tieulam-relay-public", async (req, res) => {
  await fetchTieuLamData(req, res);
});

export default router;

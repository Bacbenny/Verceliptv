import { Router, type Request, type Response, type IRouter } from "express";

const router: IRouter = Router();

const TIEULAM_FRONTEND   = process.env.TIEULAM_FRONTEND   ?? "https://sv1.tieulam1.live";
const TIEULAM_API_STATIC = process.env.TIEULAM_API         ?? "https://api.tlap12062026.xyz";
const RELAY_SECRET       = process.env.RELAY_SECRET        ?? "";
const MATCH_MAX_DURATION = parseInt(process.env.MATCH_MAX_DURATION ?? "7200", 10);

// ─── Auto-discovery: extract current API domain from TieuLam JS bundle ───────

interface DomainCache {
  domain: string;
  discoveredAt: number;
}

let domainCache: DomainCache | null = null;
const CACHE_TTL_MS = 60 * 60 * 1000; // 1 hour

async function discoverApiDomain(): Promise<string> {
  // Return cached domain if still fresh
  if (domainCache && Date.now() - domainCache.discoveredAt < CACHE_TTL_MS) {
    return domainCache.domain;
  }

  try {
    // Step 1: fetch frontend HTML to get JS bundle filename
    const htmlRes = await fetch(TIEULAM_FRONTEND, {
      headers: { "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36" },
      signal: AbortSignal.timeout(10_000),
    });
    const html = await htmlRes.text();

    // e.g. src="/assets/index-CQy9RcyN.js"
    const jsMatch = html.match(/src="(\/assets\/index-[^"]+\.js)"/);
    if (!jsMatch) throw new Error("JS bundle not found in HTML");

    // Step 2: fetch JS bundle and search for API domain pattern
    const jsUrl = `${TIEULAM_FRONTEND}${jsMatch[1]}`;
    const jsRes = await fetch(jsUrl, {
      headers: { "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36" },
      signal: AbortSignal.timeout(15_000),
    });
    const js = await jsRes.text();

    // Pattern: https://api.tlapDDMMYYYY.xyz
    const apiMatch = js.match(/https:\/\/api\.[a-z0-9]+\.xyz/);
    if (!apiMatch) throw new Error("API domain not found in JS bundle");

    const discovered = apiMatch[0];
    domainCache = { domain: discovered, discoveredAt: Date.now() };
    return discovered;
  } catch (err) {
    // Fall back to env var (or last cached value if any)
    if (domainCache) return domainCache.domain;
    return TIEULAM_API_STATIC;
  }
}

// Force a refresh on startup (non-blocking)
discoverApiDomain().catch(() => {});

// ─── Match fetching ───────────────────────────────────────────────────────────

const REQUEST_HEADERS = {
  "Accept": "application/json, text/plain, */*",
  "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
  "Content-Type": "application/json",
  "Referer": `${TIEULAM_FRONTEND}/`,
  "Origin": TIEULAM_FRONTEND,
  "sec-fetch-dest": "empty",
  "sec-fetch-mode": "cors",
  "sec-fetch-site": "cross-site",
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
};

async function fetchPage(apiUrl: string, cutoff: string, cutoffEnd: string, page: number): Promise<unknown[]> {
  try {
    const r = await fetch(apiUrl, {
      method: "POST",
      headers: REQUEST_HEADERS,
      signal: AbortSignal.timeout(12_000),
      body: JSON.stringify({
        queries: [
          { field: "start_date", type: "gte", value: cutoff },
          { field: "start_date", type: "lte", value: cutoffEnd },
        ],
        query_and: true,
        limit: 50,
        page,
        order_asc: "start_date",
      }),
    });
    if (!r.ok) return [];
    const j = (await r.json()) as { data?: unknown[] };
    return j.data ?? [];
  } catch {
    return [];
  }
}

// ─── Core relay handler ───────────────────────────────────────────────────────

/**
 * Fetches 8 pages in parallel, filters BLV client-side.
 * The TieuLam API's blv=is_not_null server-side filter is broken (returns all matches),
 * and BLV matches are scattered across all pages — scanning all pages client-side is required.
 */
async function handleRelay(req: Request, res: Response): Promise<void> {
  const now = new Date();

  const cutoff      = new Date(now.getTime() - MATCH_MAX_DURATION * 1000).toISOString().slice(0, 19);
  const cutoffEnd   = new Date(now.getTime() + 48 * 3600 * 1000).toISOString().slice(0, 19);
  const cutoffMs    = now.getTime() - MATCH_MAX_DURATION * 1000;
  const cutoffEndMs = now.getTime() + 48 * 3600 * 1000;

  const apiBase = await discoverApiDomain();
  const apiUrl  = `${apiBase.replace(/\/$/, "")}/matches/graph`;

  try {
    // Fetch pages 1-8 in parallel — BLV matches are scattered across all pages
    const pages = await Promise.all(
      [1, 2, 3, 4, 5, 6, 7, 8].map(p => fetchPage(apiUrl, cutoff, cutoffEnd, p))
    );

    // Flatten + deduplicate by id/stream_key
    const seen = new Set<string>();
    const allMatches: unknown[] = [];
    for (const page of pages) {
      for (const m of page) {
        const match = m as Record<string, unknown>;
        const key = String(match.id ?? match.stream_key ?? JSON.stringify(m));
        if (!seen.has(key)) { seen.add(key); allMatches.push(m); }
      }
    }

    // Separate BLV vs other matches, filter by time window
    const blvMatches: unknown[]   = [];
    const otherMatches: unknown[] = [];

    for (const m of allMatches) {
      const match = m as Record<string, unknown>;
      const startDate = String(match.start_date ?? "");
      const t = startDate
        ? new Date(startDate.includes("Z") ? startDate : startDate + "Z").getTime()
        : now.getTime();
      if (t < cutoffMs || t > cutoffEndMs) continue;
      if (match.blv) {
        blvMatches.push(m);
      } else {
        otherMatches.push(m);
      }
    }

    const sortByDate = (a: unknown, b: unknown) =>
      String((a as Record<string, unknown>).start_date ?? "")
        .localeCompare(String((b as Record<string, unknown>).start_date ?? ""));

    blvMatches.sort(sortByDate);
    otherMatches.sort(sortByDate);

    const combined = [...blvMatches, ...otherMatches];

    req.log?.info?.({ blv: blvMatches.length, total: combined.length, apiBase }, "TieuLam relay OK");
    res.json({ data: combined });
  } catch (err) {
    req.log?.error?.({ err }, "TieuLam relay fetch failed");
    res.status(502).json({ error: String(err), data: [] });
  }
}

// ─── Routes ──────────────────────────────────────────────────────────────────

/** /api/tieulam-relay — secured with RELAY_SECRET header */
router.get("/tieulam-relay", async (req, res) => {
  if (RELAY_SECRET) {
    const token = req.headers["x-relay-token"];
    if (token !== RELAY_SECRET) {
      res.status(401).json({ error: "Unauthorized" });
      return;
    }
  }
  await handleRelay(req, res);
});

/** /api/tieulam-relay-public — no auth, used by Vercel as default relay */
router.get("/tieulam-relay-public", async (req, res) => {
  await handleRelay(req, res);
});

/**
 * /api/tieulam-domain — debug: show currently discovered API domain + cache age
 */
router.get("/tieulam-domain", (req, res) => {
  const ageMs = domainCache ? Date.now() - domainCache.discoveredAt : null;
  res.json({
    discovered: domainCache?.domain ?? null,
    static_fallback: TIEULAM_API_STATIC,
    active: domainCache?.domain ?? TIEULAM_API_STATIC,
    cache_age_seconds: ageMs !== null ? Math.round(ageMs / 1000) : null,
    cache_ttl_seconds: CACHE_TTL_MS / 1000,
    refreshed_at: domainCache ? new Date(domainCache.discoveredAt).toISOString() : null,
  });
});

export default router;

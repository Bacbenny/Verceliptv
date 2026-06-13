import { Router, type Request, type Response, type IRouter } from "express";

const router: IRouter = Router();

const TIEULAM_FRONTEND   = process.env.TIEULAM_FRONTEND   ?? "https://sv1.tieulam1.live";
const TIEULAM_API        = process.env.TIEULAM_API         ?? "https://api.tlap12062026.xyz";
const RELAY_SECRET       = process.env.RELAY_SECRET        ?? "";
const MATCH_MAX_DURATION = parseInt(process.env.MATCH_MAX_DURATION ?? "7200", 10);

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

/**
 * Core handler — fetches 8 pages in parallel, filters BLV client-side.
 * The TieuLam API's server-side blv=is_not_null filter is broken (returns all matches),
 * so we must scan all pages and filter in relay.
 */
async function handleRelay(req: Request, res: Response): Promise<void> {
  const now = new Date();

  // 48h window: from MATCH_MAX_DURATION ago to 48h ahead
  const cutoff    = new Date(now.getTime() - MATCH_MAX_DURATION * 1000).toISOString().slice(0, 19);
  const cutoffEnd = new Date(now.getTime() + 48 * 3600 * 1000).toISOString().slice(0, 19);
  const cutoffMs  = now.getTime() - MATCH_MAX_DURATION * 1000;
  const cutoffEndMs = now.getTime() + 48 * 3600 * 1000;

  const apiUrl = `${TIEULAM_API.replace(/\/$/, "")}/matches/graph`;

  try {
    // Fetch pages 1-8 in parallel — BLV matches are scattered across all pages
    const pages = await Promise.all(
      [1, 2, 3, 4, 5, 6, 7, 8].map(p => fetchPage(apiUrl, cutoff, cutoffEnd, p))
    );

    // Flatten all pages, deduplicate by id/stream_key
    const seen = new Set<string>();
    const allMatches: unknown[] = [];
    for (const page of pages) {
      for (const m of page) {
        const match = m as Record<string, unknown>;
        const key = String(match.id ?? match.stream_key ?? JSON.stringify(m));
        if (!seen.has(key)) { seen.add(key); allMatches.push(m); }
      }
    }

    // Separate BLV matches (filter client-side since API filter is broken)
    const blvMatches: unknown[] = [];
    const otherMatches: unknown[] = [];

    for (const m of allMatches) {
      const match = m as Record<string, unknown>;
      const startDate = String(match.start_date ?? "");
      const t = startDate
        ? new Date(startDate.includes("Z") ? startDate : startDate + "Z").getTime()
        : now.getTime();
      if (t < cutoffMs || t > cutoffEndMs) continue; // outside window
      if (match.blv) {
        blvMatches.push(m);
      } else {
        otherMatches.push(m);
      }
    }

    // BLV matches first, then others — sort each group by start_date
    const sortByDate = (a: unknown, b: unknown) =>
      String((a as Record<string, unknown>).start_date ?? "")
        .localeCompare(String((b as Record<string, unknown>).start_date ?? ""));

    blvMatches.sort(sortByDate);
    otherMatches.sort(sortByDate);

    const combined = [...blvMatches, ...otherMatches];

    req.log?.info?.({ blv: blvMatches.length, total: combined.length }, "TieuLam relay OK");
    res.json({ data: combined });
  } catch (err) {
    req.log?.error?.({ err }, "TieuLam relay fetch failed");
    res.status(502).json({ error: String(err), data: [] });
  }
}

/**
 * /api/tieulam-relay — secured with RELAY_SECRET header
 */
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

/**
 * /api/tieulam-relay-public — không cần auth
 * Vercel dùng endpoint này làm fallback khi TIEULAM_RELAY_URL chưa được set
 */
router.get("/tieulam-relay-public", async (req, res) => {
  await handleRelay(req, res);
});

export default router;

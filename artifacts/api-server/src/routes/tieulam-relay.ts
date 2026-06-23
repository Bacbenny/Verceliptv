import { Router } from "express";
import fetch from "node-fetch";

const router = Router();

const TIEULAM_API      = "https://api.tlap17062026.com/matches/graph";
const TIEULAM_FRONTEND = "https://sv2.tieulam.info";

const HEADERS = {
  "Content-Type":   "application/json",
  "Accept":         "application/json, text/plain, */*",
  "Accept-Language":"vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
  "Origin":         TIEULAM_FRONTEND,
  "Referer":        TIEULAM_FRONTEND + "/",
  "User-Agent":     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
  "sec-fetch-dest": "empty",
  "sec-fetch-mode": "cors",
  "sec-fetch-site": "cross-site",
};

/**
 * Public TieuLam relay — no auth required.
 * Replit IPs are not blocked by TieuLam's Cloudflare, so this proxies for
 * Vercel/Render deployments that would get 403 calling the API directly.
 *
 * Usage: set TIEULAM_RELAY_URL=https://<your-replit-app>/api/tieulam-relay-public
 * No RELAY_SECRET needed.
 */
router.get("/tieulam-relay-public", async (req, res) => {
  try {
    const now       = new Date();
    const cutoff    = new Date(now.getTime() - 2 * 3600 * 1000);
    const cutoffEnd = new Date(now.getTime() + 72 * 3600 * 1000);

    const fmt = (d: Date) => d.toISOString().slice(0, 19);

    const payload = {
      queries: [
        { field: "start_date", type: "gte", value: fmt(cutoff) },
        { field: "start_date", type: "lte", value: fmt(cutoffEnd) },
      ],
      query_and: true,
      limit: 100,
      page:  1,
      order_asc: "start_date",
    };

    const r = await fetch(TIEULAM_API, {
      method:  "POST",
      headers: HEADERS,
      body:    JSON.stringify(payload),
      // node-fetch v2 doesn't have built-in timeout; use AbortController
      signal:  AbortSignal.timeout(15000),
    });

    if (!r.ok) {
      req.log.warn({ status: r.status }, "TieuLam upstream error");
      return res.status(r.status).json({ error: `TieuLam API returned ${r.status}` });
    }

    const data = (await r.json()) as Record<string, unknown>;
    return res.json(data);
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    req.log.error({ err: msg }, "TieuLam relay fetch error");
    return res.status(502).json({ error: msg });
  }
});

export default router;

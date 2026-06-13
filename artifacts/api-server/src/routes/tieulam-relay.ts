import { Router, type IRouter } from "express";

const router: IRouter = Router();

const TIEULAM_FRONTEND   = process.env.TIEULAM_FRONTEND   ?? "https://sv1.tieulam1.live";
const TIEULAM_API        = process.env.TIEULAM_API         ?? "https://api.tlap12062026.xyz";
const RELAY_SECRET       = process.env.RELAY_SECRET        ?? "";
const MATCH_MAX_DURATION = parseInt(process.env.MATCH_MAX_DURATION ?? "7200", 10);

router.get("/tieulam-relay", async (req, res) => {
  if (RELAY_SECRET) {
    const token = req.headers["x-relay-token"];
    if (token !== RELAY_SECRET) {
      res.status(401).json({ error: "Unauthorized" });
      return;
    }
  }

  const now = new Date();
  const cutoff = new Date(now.getTime() - MATCH_MAX_DURATION * 1000)
    .toISOString()
    .slice(0, 19);
  const cutoffEnd = new Date(now.getTime() + 24 * 3600 * 1000)
    .toISOString()
    .slice(0, 19);

  const apiUrl = `${TIEULAM_API.replace(/\/$/, "")}/matches/graph`;
  const headers = {
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

  // Query A: trận đang/vừa live trong khung giờ (có source_live)
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

  // Query B: trận có BLV — KHÔNG thêm time filter (kết hợp time+blv phá filter API)
  // Time filtering sẽ được thực hiện ở relay code bên dưới
  const blvEnd = now.getTime() + 48 * 3600 * 1000; // 48h window (ms) cho BLV
  const blvPayload = {
    queries: [{ field: "blv", type: "is_not_null", value: "" }],
    query_and: true,
    limit: 50,
    page: 1,
    // No order_asc, no time filter — both break the blv filter in TieuLam API
  };

  try {
    const [r1, r2, rBlv] = await Promise.all([
      fetch(apiUrl, { method: "POST", headers, body: JSON.stringify(timeWindowPayload(1)) }),
      fetch(apiUrl, { method: "POST", headers, body: JSON.stringify(timeWindowPayload(2)) }),
      fetch(apiUrl, { method: "POST", headers, body: JSON.stringify(blvPayload) }),
    ]);

    if (!r1.ok) {
      req.log.warn({ status: r1.status }, "TieuLam upstream error");
      res.status(502).json({ error: `Upstream ${r1.status}`, data: [] });
      return;
    }

    const j1    = (await r1.json()) as { data?: unknown[] };
    const j2    = r2.ok   ? ((await r2.json())   as { data?: unknown[] }) : { data: [] };
    const jBlv  = rBlv.ok ? ((await rBlv.json()) as { data?: unknown[] }) : { data: [] };

    // Filter BLV matches by time window in relay (API-side time filter breaks blv filter)
    const cutoffMs  = now.getTime() - MATCH_MAX_DURATION * 1000;
    const blvEndMs  = blvEnd;
    const blvMatches = (jBlv.data ?? []).filter(m => {
      const match = m as Record<string, unknown>;
      if (!match.blv) return false; // only keep truly non-null blv
      const startDate = String(match.start_date ?? "");
      if (!startDate) return true;
      const t = new Date(startDate.includes("Z") ? startDate : startDate + "Z").getTime();
      return t >= cutoffMs && t <= blvEndMs;
    });

    const seen = new Set<string>();
    const combined: unknown[] = [];

    // BLV matches first — ensures blv field is preserved when deduplicating
    for (const m of [...blvMatches, ...(j1.data ?? []), ...(j2.data ?? [])]) {
      const match = m as Record<string, unknown>;
      const key = String(match.id ?? match.stream_key ?? JSON.stringify(m));
      if (!seen.has(key)) { seen.add(key); combined.push(m); }
    }

    res.json({ data: combined });
  } catch (err) {
    req.log.error({ err }, "TieuLam relay fetch failed");
    res.status(502).json({ error: String(err), data: [] });
  }
});

export default router;

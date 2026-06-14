import { Router, type IRouter } from "express";

const router: IRouter = Router();

const TIEULAM_FRONTEND_URL =
  process.env["TIEULAM_FRONTEND"] ?? "https://sv1.tieulam1.live";
const TIEULAM_KNOWN_API_BASE =
  process.env["TIEULAM_API"] ?? "https://api.tlap12062026.xyz";
const RELAY_SECRET = process.env["RELAY_SECRET"] ?? "";
const MATCH_MAX_AGE_SECONDS = parseInt(
  process.env["MATCH_MAX_DURATION"] ?? "7200",
  10,
);

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
      const jsRes = await fetch(
        TIEULAM_FRONTEND_URL.replace(/\/$/, "") + jsPath,
        {
          headers: { "User-Agent": FETCH_HEADERS["User-Agent"]! },
          signal: AbortSignal.timeout(15_000),
        },
      );
      const js = await jsRes.text();
      const hit =
        js.match(/create\(\{baseURL:"(https:\/\/[^"]+)"\}/)?.at(1) ??
        js.match(/baseURL:"(https:\/\/[^"]{10,60})"/)?.at(1);
      if (hit) return hit.replace(/\/$/, "") + "/matches/graph";
    }
  } catch {
  }
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

async function fetchMatches(): Promise<unknown[]> {
  const cutoffMs = Date.now() - MATCH_MAX_AGE_SECONDS * 1000;
  const cutoffEndMs = Date.now() + 72 * 3600 * 1000;

  const toIso = (ms: number) =>
    new Date(ms).toISOString().replace(/\.\d+Z$/, "");

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

  let apiUrl = await getApiUrl();

  const tryFetch = async (url: string): Promise<unknown[]> => {
    const res = await fetch(url, {
      method: "POST",
      headers: FETCH_HEADERS,
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(15_000),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const json = (await res.json()) as { data?: unknown[] };
    return json.data ?? [];
  };

  try {
    return await tryFetch(apiUrl);
  } catch {
    _apiDiscoveredAt = 0;
    apiUrl = await getApiUrl();
    return await tryFetch(apiUrl);
  }
}

router.get("/tieulam-relay-public", async (req, res) => {
  if (RELAY_SECRET) {
    const token = req.headers["x-relay-token"];
    if (token !== RELAY_SECRET) {
      res.status(403).json({ error: "Forbidden" });
      return;
    }
  }

  try {
    const data = await fetchMatches();
    res.json({ data, fetched_at: new Date().toISOString() });
  } catch (err) {
    req.log.error({ err }, "tieulam-relay fetch failed");
    res.status(502).json({ error: "Upstream fetch failed" });
  }
});

export default router;

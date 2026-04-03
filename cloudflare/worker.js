/**
 * AlphaAgents Fetch Proxy Worker
 *
 * Deploy to Cloudflare Workers to proxy news fetching requests.
 * Each invocation runs on an edge node with a different IP, providing
 * natural IP rotation for anti-scraping.
 *
 * Free tier: 100,000 requests/day — more than enough for news fetching.
 *
 * Usage:
 *   POST https://your-worker.workers.dev/
 *   Body: { "url": "https://target.com/api", "method": "GET", "headers": {...} }
 *
 * Deploy:
 *   npx wrangler deploy cloudflare/worker.js --name alpha-fetch-proxy
 *
 * Security:
 *   Set AUTH_TOKEN secret via `npx wrangler secret put AUTH_TOKEN`
 *   Then set CF_WORKER_AUTH_TOKEN env var in your .env
 */

// Allowed domains — only proxy requests to known news sources
const ALLOWED_DOMAINS = new Set([
  "newsapi.eastmoney.com",
  "np-listapi.eastmoney.com",
  "search-api-web.eastmoney.com",
  "www.cls.cn",
  "api-one-wscn.awtmt.com",
  "flash-api.jin10.com",
  "www.pbc.gov.cn",
  "www.news.cn",
  "feeds.bbci.co.uk",
  "search.cnbc.com",
  "news.google.com",
  "www.whitehouse.gov",
  "www.federalreserve.gov",
  "www.sec.gov",
  "rsshub.app",
  "nitter.net",
]);

const ALLOWED_METHODS = new Set(["GET", "HEAD"]);
const MAX_TIMEOUT = 30000;

export default {
  async fetch(request, env) {
    // CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "POST",
          "Access-Control-Allow-Headers": "Content-Type, Authorization",
        },
      });
    }

    if (request.method !== "POST") {
      return Response.json({ error: "POST only" }, { status: 405 });
    }

    // Auth check — required in production
    if (env.AUTH_TOKEN) {
      const auth = request.headers.get("Authorization");
      if (auth !== `Bearer ${env.AUTH_TOKEN}`) {
        return Response.json({ error: "unauthorized" }, { status: 401 });
      }
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return Response.json({ error: "invalid JSON body" }, { status: 400 });
    }

    const { url, method = "GET", headers = {}, timeout = 15000 } = body;

    if (!url) {
      return Response.json({ error: "url required" }, { status: 400 });
    }

    // Validate URL — only allow HTTPS/HTTP to known domains
    let parsed;
    try {
      parsed = new URL(url);
    } catch {
      return Response.json({ error: "invalid url" }, { status: 400 });
    }

    if (!["http:", "https:"].includes(parsed.protocol)) {
      return Response.json({ error: "only http/https allowed" }, { status: 400 });
    }

    if (!ALLOWED_DOMAINS.has(parsed.hostname)) {
      return Response.json(
        { error: `domain not allowed: ${parsed.hostname}` },
        { status: 403 },
      );
    }

    // Validate method — only GET/HEAD for news fetching
    const safeMethod = method.toUpperCase();
    if (!ALLOWED_METHODS.has(safeMethod)) {
      return Response.json({ error: `method not allowed: ${method}` }, { status: 405 });
    }

    // Cap timeout
    const safeTimeout = Math.min(Number(timeout) || 15000, MAX_TIMEOUT);

    // Strip sensitive headers from passthrough
    const safeHeaders = {};
    const BLOCKED_HEADERS = new Set(["host", "authorization", "cookie", "x-forwarded-for"]);
    for (const [k, v] of Object.entries(headers)) {
      if (!BLOCKED_HEADERS.has(k.toLowerCase())) {
        safeHeaders[k] = v;
      }
    }

    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), safeTimeout);

      const resp = await fetch(url, {
        method: safeMethod,
        headers: {
          "User-Agent": safeHeaders["User-Agent"] || "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
          ...safeHeaders,
        },
        signal: controller.signal,
        redirect: "follow",
      });

      clearTimeout(timer);

      const responseBody = await resp.text();

      return Response.json({
        status: resp.status,
        headers: Object.fromEntries(resp.headers),
        body: responseBody,
        cf: {
          colo: request.cf?.colo || "unknown",
          country: request.cf?.country || "unknown",
        },
      });
    } catch (err) {
      return Response.json(
        { error: err.message, type: err.name },
        { status: 502 },
      );
    }
  },
};

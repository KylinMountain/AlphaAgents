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

    // Auth check
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

    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeout);

      const resp = await fetch(url, {
        method,
        headers: {
          "User-Agent": headers["User-Agent"] || "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
          ...headers,
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

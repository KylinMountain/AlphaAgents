export default {
  async fetch(request) {
    if (request.method !== "POST") {
      return Response.json({ error: "POST only" }, { status: 405 });
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return Response.json({ error: "invalid JSON" }, { status: 400 });
    }

    const { url, method = "GET", headers = {}, timeout = 15000 } = body;
    if (!url) {
      return Response.json({ error: "missing url" }, { status: 400 });
    }

    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeout);

      const resp = await fetch(url, {
        method,
        headers,
        signal: controller.signal,
        redirect: "follow",
      });
      clearTimeout(timer);

      const respBody = await resp.text();

      return Response.json({
        status: resp.status,
        body: respBody,
        headers: Object.fromEntries(resp.headers.entries()),
        cf: request.cf || {},
      });
    } catch (err) {
      return Response.json({ error: err.message }, { status: 502 });
    }
  },
};

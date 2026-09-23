const RENDER_ORIGIN = "https://wikigraph.onrender.com";

function isApiRequest(pathname) {
  return pathname === "/api" || pathname.startsWith("/api/");
}

function forwardedHeaders(request) {
  const headers = new Headers(request.headers);
  // The browser's Host identifies Cloudflare, not the Render origin. Let the
  // platform create the correct Host and content-length headers for the new URL.
  headers.delete("host");
  headers.delete("content-length");
  return headers;
}

export async function proxyApi(request) {
  const incoming = new URL(request.url);
  const target = new URL(incoming.pathname + incoming.search, RENDER_ORIGIN);
  const init = {
    method: request.method,
    headers: forwardedHeaders(request),
    redirect: "manual",
  };
  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = request.body;
  }

  try {
    const response = await fetch(target, init);
    // Returning the origin stream directly keeps SSE incremental and preserves
    // the origin status and content headers without buffering the Graph API.
    return new Response(response.body, response);
  } catch {
    return new Response("Render origin unavailable", {
      status: 502,
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  }
}

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (isApiRequest(pathname)) return proxyApi(request);
    return env.ASSETS.fetch(request);
  },
};

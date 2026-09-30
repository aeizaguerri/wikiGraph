import assert from "node:assert/strict";
import test from "node:test";
import worker, { proxyApi } from "./worker.js";

test("proxyApi preserves method, query, body, auth headers, status, and stream", async () => {
  const originalFetch = globalThis.fetch;
  let captured;
  globalThis.fetch = async (url, init) => {
    captured = { url: String(url), init };
    return new Response("data: progress\n\n", {
      status: 202,
      headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
    });
  };

  try {
    const request = new Request("https://frontend.example/api/runs?run=abc", {
      method: "POST",
      headers: { authorization: "Bearer browser-token", "content-type": "application/json" },
      body: JSON.stringify({ seed: "Graph" }),
    });
    const response = await proxyApi(request);
    assert.equal(captured.url, "https://wikigraph.onrender.com/api/runs?run=abc");
    assert.equal(captured.init.method, "POST");
    assert.equal(captured.init.headers.get("authorization"), "Bearer browser-token");
    assert.equal(captured.init.headers.get("host"), null);
    assert.equal(await new Response(captured.init.body).text(), '{"seed":"Graph"}');
    assert.equal(response.status, 202);
    assert.equal(response.headers.get("content-type"), "text/event-stream");
    assert.equal(await response.text(), "data: progress\n\n");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("non-API requests use Cloudflare assets, including SPA routes", async () => {
  const assets = { fetch: async (request) => new Response(new URL(request.url).pathname) };
  const response = await worker.fetch(
    new Request("https://frontend.example/runs/abc?color=level"),
    { ASSETS: assets },
  );
  assert.equal(await response.text(), "/runs/abc");
});

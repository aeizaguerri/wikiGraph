import assert from "node:assert/strict";
import test from "node:test";
import worker from "./worker.js";

const unavailableAssets = {
  fetch() {
    assert.fail("API requests must not fall back to static assets");
  },
};

test("API requests preserve method, query, body, status and safe headers", async (t) => {
  for (const path of ["/api", "/api/runs?run=abc"]) {
    await t.test(path, async (t) => {
      let captured;
      t.mock.method(globalThis, "fetch", async (url, init) => {
        captured = { url: String(url), init };
        return new Response("data: progress\n\n", {
          status: 202,
          headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
        });
      });

      const response = await worker.fetch(new Request(`https://frontend.example${path}`, {
        method: "POST",
        headers: {
          authorization: "Bearer browser-token",
          "content-type": "application/json",
          host: "frontend.example",
          "content-length": "16",
        },
        body: '{"seed":"Graph"}',
      }), { ASSETS: unavailableAssets });

      assert.equal(captured.url, `https://wikigraph.onrender.com${path}`);
      assert.equal(captured.init.method, "POST");
      assert.equal(captured.init.redirect, "manual");
      assert.equal(captured.init.headers.get("authorization"), "Bearer browser-token");
      assert.equal(captured.init.headers.get("content-type"), "application/json");
      assert.equal(captured.init.headers.get("host"), null);
      assert.equal(captured.init.headers.get("content-length"), null);
      assert.equal(await new Response(captured.init.body).text(), '{"seed":"Graph"}');
      assert.equal(response.status, 202);
      assert.equal(response.headers.get("content-type"), "text/event-stream");
      assert.equal(response.headers.get("cache-control"), "no-cache");
      assert.equal(await response.text(), "data: progress\n\n");
    });
  }
});

test("GET and HEAD reach the API without a request body", async (t) => {
  for (const method of ["GET", "HEAD"]) {
    await t.test(method, async (t) => {
      t.mock.method(globalThis, "fetch", async (url, init) => {
        assert.equal(String(url), "https://wikigraph.onrender.com/api/runs/abc");
        assert.equal(init.method, method);
        assert.ok(init.body == null);
        return new Response(null, { status: 204 });
      });
      const response = await worker.fetch(new Request("https://frontend.example/api/runs/abc", {
        method,
      }), { ASSETS: unavailableAssets });
      assert.equal(response.status, 204);
      assert.equal(await response.text(), "");
    });
  }
});

test("API events arrive before the origin stream closes", { timeout: 1500 }, async (t) => {
  let origin;
  const encoder = new TextEncoder();
  const stream = new ReadableStream({ start(controller) { origin = controller; } });
  t.mock.method(globalThis, "fetch", async () => new Response(stream, {
    headers: { "content-type": "text/event-stream" },
  }));
  origin.enqueue(encoder.encode("data: progress\n\n"));

  const response = await worker.fetch(new Request("https://frontend.example/api/runs/abc/events"), {
    ASSETS: unavailableAssets,
  });
  const reader = response.body.getReader();
  const first = await reader.read();
  assert.equal(first.done, false);
  assert.equal(new TextDecoder().decode(first.value), "data: progress\n\n");

  origin.enqueue(encoder.encode("data: completed\n\n"));
  origin.close();
  const last = await reader.read();
  assert.equal(last.done, false);
  assert.equal(new TextDecoder().decode(last.value), "data: completed\n\n");
  assert.deepEqual(await reader.read(), { done: true, value: undefined });
});

test("an unavailable origin returns 502 rather than static assets", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new TypeError("connection failed"); });
  const response = await worker.fetch(new Request("https://frontend.example/api/runs/abc"), {
    ASSETS: unavailableAssets,
  });
  assert.equal(response.status, 502);
  assert.equal(response.headers.get("content-type"), "text/plain; charset=utf-8");
  assert.equal(await response.text(), "Render origin unavailable");
});

test("non-API requests use Cloudflare assets, including SPA routes", async (t) => {
  for (const path of ["/", "/runs/abc?color=level", "/apiary"]) {
    await t.test(path, async (t) => {
      t.mock.method(globalThis, "fetch", () => assert.fail("Asset requests must not reach Render"));
      const response = await worker.fetch(new Request(`https://frontend.example${path}`), {
        ASSETS: {
          fetch(request) {
            assert.equal(request.url, `https://frontend.example${path}`);
            return new Response("static asset", { status: 200 });
          },
        },
      });
      assert.equal(response.status, 200);
      assert.equal(await response.text(), "static asset");
    });
  }
});

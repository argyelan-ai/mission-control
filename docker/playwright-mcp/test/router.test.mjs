// Router tests (ADR-088 router step): one playwright-mcp child per browser
// session, so every session's tabs are opened over its own gateway address.
// Run: node --test docker/playwright-mcp/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { createRouter } from "../router.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FAKE = path.join(HERE, "fake-playwright-mcp.mjs");
const TOKEN = "tok_" + "A".repeat(40);
const OTHER = "tok_" + "B".repeat(40);
const GATEWAY = "http://cdp-browser:9300";
const LEGACY = "http://172.30.99.10:9223";

async function start(options = {}) {
  const lines = [];
  const router = createRouter({
    gatewayUrl: GATEWAY,
    legacyCdpEndpoint: LEGACY,
    childCommand: [process.execPath, FAKE],
    childArgs: ["--isolated", "--viewport-size", "1280x800", "--output-dir", "/output"],
    log: (line) => lines.push(line),
    ...options,
  });
  await router.listen(0, "127.0.0.1");
  const base = `http://127.0.0.1:${router.port}`;
  return { router, base, lines };
}

function request(url, { method = "POST", headers = {}, body = method === "POST" ? "{}" : null } = {}) {
  // Raw path on purpose: `new URL()` would normalise "/a/../mcp" before the
  // router ever sees it.
  const { hostname, port } = new URL(url);
  const rawPath = url.slice(url.indexOf("/", "http://".length));
  return new Promise((resolve, reject) => {
    const opts = { hostname, port, path: rawPath, method, headers: { "content-type": "application/json", ...headers } };
    const req = http.request(opts, (res) => {
      const chunks = [];
      const arrivals = [];
      res.on("data", (c) => {
        chunks.push(c);
        arrivals.push(Date.now());
      });
      res.on("end", () => resolve({ status: res.statusCode, headers: res.headers, text: Buffer.concat(chunks).toString(), arrivals }));
    });
    req.on("error", reject);
    if (body) req.write(body);
    req.end();
  });
}

const json = (r) => JSON.parse(r.text);

test("a session address gets its own child on the session's gateway address", async (t) => {
  const { router, base } = await start();
  t.after(() => router.close());
  const r = await request(`${base}/s/${TOKEN}/mcp`, { body: '{"jsonrpc":"2.0","id":1}' });
  assert.equal(r.status, 200);
  const seen = json(r);
  assert.equal(seen.cdp, `${GATEWAY}/s/${TOKEN}/`);
  assert.equal(seen.path, "/mcp");                         // prefix stripped for the child
  assert.equal(seen.body, '{"jsonrpc":"2.0","id":1}');
  assert.ok(seen.args.includes("--isolated"));
  assert.equal(seen.args[seen.args.indexOf("--host") + 1], "127.0.0.1");  // child never on the network
});

test("one child per session, reused across requests", async (t) => {
  const { router, base } = await start();
  t.after(() => router.close());
  const a1 = json(await request(`${base}/s/${TOKEN}/mcp`));
  const a2 = json(await request(`${base}/s/${TOKEN}/mcp`));
  const b = json(await request(`${base}/s/${OTHER}/mcp`));
  assert.equal(a1.pid, a2.pid);
  assert.notEqual(a1.pid, b.pid);
  assert.equal(b.cdp, `${GATEWAY}/s/${OTHER}/`);
  assert.equal(router.childCount(), 2);
});

test("an agent address and the legacy /mcp keep working", async (t) => {
  const { router, base } = await start();
  t.after(() => router.close());
  assert.equal(json(await request(`${base}/a/alpha/mcp`)).cdp, `${GATEWAY}/a/alpha/`);
  // Unchanged for every existing config: one shared child on the old endpoint.
  assert.equal(json(await request(`${base}/mcp`)).cdp, LEGACY);
});

test("malformed tokens and slugs are refused without starting anything", async (t) => {
  const { router, base } = await start();
  t.after(() => router.close());
  for (const p of ["/s/short/mcp", "/s/../../etc/mcp", "/a/Not_A_Slug!/mcp", "/a/../mcp", "/s//mcp", "/other"]) {
    const r = await request(base + p);
    assert.equal(r.status, 404, p);
  }
  assert.equal(router.childCount(), 0);
});

test("MCP session headers and DELETE pass through both ways", async (t) => {
  const { router, base } = await start();
  t.after(() => router.close());
  const r = await request(`${base}/s/${TOKEN}/mcp`, { headers: { "mcp-session-id": "abc" } });
  assert.equal(json(r).sessionHeader, "abc");
  assert.equal(r.headers["mcp-session-id"], "child-session");
  const d = await request(`${base}/s/${TOKEN}/mcp`, { method: "DELETE", headers: { "mcp-session-id": "abc" } });
  assert.equal(json(d).method, "DELETE");
  assert.equal(router.childCount(), 1);                     // an MCP DELETE ends the MCP session, not the child
});

test("event streams are passed through as they arrive", async (t) => {
  const { router, base } = await start();
  t.after(() => router.close());
  const r = await request(`${base}/s/${TOKEN}/mcp`, { headers: { "x-test-sse": "1", accept: "text/event-stream" } });
  assert.match(r.text, /data: first[\s\S]*data: second/);
  assert.ok(r.arrivals.length >= 2 && r.arrivals.at(-1) - r.arrivals[0] >= 200, "streamed, not buffered");
});

test("an idle child is stopped and comes back on the next request", async (t) => {
  const { router, base } = await start({ idleMs: 300 });
  t.after(() => router.close());
  const first = json(await request(`${base}/s/${TOKEN}/mcp`));
  await new Promise((r) => setTimeout(r, 700));
  assert.equal(router.childCount(), 0);
  const second = json(await request(`${base}/s/${TOKEN}/mcp`));
  assert.notEqual(first.pid, second.pid);
});

test("a child that died is started again on the next request", async (t) => {
  const { router, base } = await start();
  t.after(() => router.close());
  const first = json(await request(`${base}/s/${TOKEN}/mcp`, { headers: { "x-test-exit": "1" } }));
  await new Promise((r) => setTimeout(r, 200));
  const second = json(await request(`${base}/s/${TOKEN}/mcp`));
  assert.notEqual(first.pid, second.pid);
});

test("the child limit answers 503 instead of starting yet another browser client", async (t) => {
  const { router, base } = await start({ maxChildren: 1 });
  t.after(() => router.close());
  assert.equal((await request(`${base}/s/${TOKEN}/mcp`)).status, 200);
  assert.equal((await request(`${base}/s/${OTHER}/mcp`)).status, 503);
});

test("ending a session stops its child (control path, token required)", async (t) => {
  const { router, base } = await start();
  t.after(() => router.close());
  await request(`${base}/s/${TOKEN}/mcp`);
  assert.equal((await request(`${base}/_router/sessions/${OTHER}`, { method: "DELETE" })).status, 404);
  assert.equal((await request(`${base}/_router/sessions/${TOKEN}`, { method: "DELETE" })).status, 200);
  assert.equal(router.childCount(), 0);
});

test("health answers without starting a child", async (t) => {
  const { router, base } = await start();
  t.after(() => router.close());
  const r = await request(`${base}/healthz`, { method: "GET" });
  assert.equal(r.status, 200);
  assert.equal(router.childCount(), 0);
});

test("the token never appears in the router's log, not even from a child", async (t) => {
  const { router, base, lines } = await start();
  t.after(() => router.close());
  await request(`${base}/s/${TOKEN}/mcp`);
  await request(`${base}/_router/sessions/${TOKEN}`, { method: "DELETE" });
  await new Promise((r) => setTimeout(r, 100));
  assert.ok(lines.length > 0, "the child's own output is forwarded");
  for (const line of lines) assert.ok(!line.includes(TOKEN), line);
});

test("two first requests at the same moment share one child", async (t) => {
  // A slow port allocation widens the window between "no child yet" and
  // "child registered" — the race a burst of first requests hits.
  const slowPort = async () => {
    await new Promise((r) => setTimeout(r, 50));
    return new Promise((resolve) => {
      const srv = http.createServer().listen(0, "127.0.0.1", () => {
        const { port } = srv.address();
        srv.close(() => resolve(port));
      });
    });
  };
  const { router, base } = await start({ allocatePort: slowPort });
  t.after(() => router.close());
  const [a, b, c] = await Promise.all([1, 2, 3].map(() => request(`${base}/s/${TOKEN}/mcp`)));
  assert.equal(new Set([a, b, c].map((r) => json(r).pid)).size, 1);
  assert.equal(router.childCount(), 1);
});

test("an unknown router flag is refused instead of starting a server that never exits", async () => {
  // CI ran `docker run <image> --version` after the entrypoint became the
  // router: it started listening and the job hung for half an hour.
  const { parseCli } = await import("../router.mjs");
  assert.throws(() => parseCli(["--version"]), /unknown router flag/);
  assert.equal(parseCli(["--listen", "9000", "--", "--version"]).childArgs[0], "--version");
});

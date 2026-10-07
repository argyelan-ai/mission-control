// Router tests (ADR-088 router step): one playwright-mcp child per browser
// session, so every session's tabs are opened over its own gateway address.
// Run: node --test docker/playwright-mcp/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { createRouter, gatewayKnowsSession } from "../router.mjs";

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
    checkSession: async () => true, // the gateway knows every test token unless a test says otherwise
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
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// An event stream left open: `first` resolves with the first chunk, `ended`
// with how the response ended ("end" | "aborted") — or never, if it hangs.
function openStream(url, headers = {}) {
  const { hostname, port, pathname } = new URL(url);
  let req;
  const first = new Promise((resolveFirst, rejectFirst) => {
    req = http.request({ hostname, port, path: pathname, method: "GET", headers: { accept: "text/event-stream", "x-test-sse-hold": "1", ...headers } });
    req.on("error", rejectFirst);
    req.end();
  });
  let resolveFirst;
  let resolveEnded;
  const firstChunk = new Promise((r) => (resolveFirst = r));
  const ended = new Promise((r) => (resolveEnded = r));
  req.on("response", (res) => {
    res.once("data", (c) => resolveFirst(String(c)));
    res.on("end", () => resolveEnded("end"));
    res.on("aborted", () => resolveEnded("aborted"));
    res.on("error", () => resolveEnded("aborted"));
    res.on("close", () => resolveEnded(res.complete ? "end" : "aborted"));
  });
  req.on("error", () => resolveEnded("aborted"));
  first.catch(() => {});
  return { first: firstChunk, ended, abort: () => req.destroy() };
}

const within = (promise, ms) => Promise.race([promise, sleep(ms).then(() => "HUNG")]);

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

// ── review R1: the shared /mcp child is exactly as before ──────────────────

test("the shared child is never stopped for idleness", async (t) => {
  const { router, base } = await start({ idleMs: 200 });
  t.after(() => router.close());
  const first = json(await request(`${base}/mcp`));
  await sleep(600);
  assert.equal(router.childCount(), 1);
  assert.equal(json(await request(`${base}/mcp`)).pid, first.pid);   // same child, same MCP sessions
});

test("the shared child does not count against the child limit", async (t) => {
  const { router, base } = await start({ maxChildren: 1 });
  t.after(() => router.close());
  assert.equal((await request(`${base}/mcp`)).status, 200);
  assert.equal((await request(`${base}/s/${TOKEN}/mcp`)).status, 200);  // the shared one took no slot
  assert.equal((await request(`${base}/s/${OTHER}/mcp`)).status, 503);
  assert.equal((await request(`${base}/mcp`)).status, 200);           // a full limit never blocks /mcp
});

test("a token the gateway does not know starts no child", async (t) => {
  const asked = [];
  const { router, base, lines } = await start({
    checkSession: async (token) => {
      asked.push(token);
      return token === TOKEN;
    },
  });
  t.after(() => router.close());
  const r = await request(`${base}/s/${OTHER}/mcp`);
  assert.equal(r.status, 404);
  assert.equal(router.childCount(), 0);
  assert.equal((await request(`${base}/s/${TOKEN}/mcp`)).status, 200);
  await request(`${base}/s/${TOKEN}/mcp`);
  assert.deepEqual(asked, [OTHER, TOKEN]);                          // checked once per child start, not per request
  for (const line of lines) assert.ok(!line.includes(OTHER), line);
});

test("a gateway that cannot answer means 502, no child", async (t) => {
  const { router, base } = await start({
    checkSession: async () => {
      throw new Error("connect refused");
    },
  });
  t.after(() => router.close());
  assert.equal((await request(`${base}/s/${TOKEN}/mcp`)).status, 502);
  assert.equal(router.childCount(), 0);
});

test("the gateway check reads 404 as unknown and 200 as known", async (t) => {
  const seen = [];
  const gw = http.createServer((req, res) => {
    seen.push(req.url);
    const status = req.url.includes(TOKEN) ? 200 : req.url.includes(OTHER) ? 404 : 500;
    res.writeHead(status).end("{}");
  });
  await new Promise((r) => gw.listen(0, "127.0.0.1", r));
  t.after(() => gw.close());
  const url = `http://127.0.0.1:${gw.address().port}/`;
  assert.equal(await gatewayKnowsSession(url, TOKEN), true);
  assert.equal(await gatewayKnowsSession(url, OTHER), false);
  await assert.rejects(gatewayKnowsSession(url, "tok_" + "C".repeat(40)), /500/);
  assert.equal(seen[0], `/s/${TOKEN}/json/version`);
});

// ── review R2: a child that goes away never leaves a client hanging ────────

test("an open stream keeps its child from idling out", async (t) => {
  const { router, base } = await start({ idleMs: 200 });
  t.after(() => router.close());
  const stream = openStream(`${base}/s/${TOKEN}/mcp`);
  await stream.first;
  await sleep(600);
  assert.equal(router.childCount(), 1);                              // still streaming = still active
  stream.abort();
  await sleep(500);
  assert.equal(router.childCount(), 0);                              // idle counts from the last close
});

test("ending a session aborts its open streams", async (t) => {
  const { router, base } = await start();
  t.after(() => router.close());
  const stream = openStream(`${base}/s/${TOKEN}/mcp`);
  await stream.first;
  assert.equal((await request(`${base}/_router/sessions/${TOKEN}`, { method: "DELETE" })).status, 200);
  assert.equal(await within(stream.ended, 2000), "aborted");
});

test("a child that crashes mid-stream aborts the client's response", async (t) => {
  const { router, base } = await start();
  t.after(() => router.close());
  const stream = openStream(`${base}/s/${TOKEN}/mcp`, { "x-test-exit-after-ms": "100" });
  await stream.first;
  assert.equal(await within(stream.ended, 2000), "aborted");
  await sleep(100);
  assert.equal(router.childCount(), 0);
});

// ── review R3: one port, one child ─────────────────────────────────────────

async function realFreePort() {
  return new Promise((resolve) => {
    const srv = net.createServer().listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

test("a port handed out twice never serves two sessions from one child", async (t) => {
  // The allocator offers the same port again while the first child holds it
  // (what the OS does when it is asked between "port free" and "child bound").
  const fixed = await realFreePort();
  let calls = 0;
  const allocatePort = async () => (++calls <= 2 ? fixed : realFreePort());
  const { router, base } = await start({ allocatePort });
  t.after(() => router.close());
  const a = json(await request(`${base}/s/${TOKEN}/mcp`));
  const b = json(await request(`${base}/s/${OTHER}/mcp`));
  assert.notEqual(a.pid, b.pid);
  assert.equal(b.cdp, `${GATEWAY}/s/${OTHER}/`);                     // B is answered by B's own child
  assert.notEqual(Number(b.args[b.args.indexOf("--port") + 1]), fixed);
});

test("a port somebody else listens on is not handed to a child", async (t) => {
  const foreign = net.createServer((sock) => {
    sock.on("error", () => {}); // the router's probe hangs up right away
    sock.end("HTTP/1.1 200 OK\r\ncontent-length: 7\r\n\r\nforeign");
  });
  await new Promise((r) => foreign.listen(0, "127.0.0.1", r));
  t.after(() => foreign.close());
  let calls = 0;
  const allocatePort = async () => (++calls === 1 ? foreign.address().port : realFreePort());
  const { router, base, lines } = await start({ allocatePort });
  t.after(() => router.close());
  const r = await request(`${base}/s/${TOKEN}/mcp`);
  assert.equal(json(r).cdp, `${GATEWAY}/s/${TOKEN}/`);
  assert.equal(calls, 2);
  assert.ok(!lines.some((l) => /retrying/.test(l)), "skipped before a child was started on it");
});

test("a child that exits while starting is started again on a fresh port", async (t) => {
  const marker = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "router-")), "failed");
  const { router, base, lines } = await start({ childArgs: ["--isolated", "--test-fail-once", marker] });
  t.after(() => router.close());
  const r = await request(`${base}/s/${TOKEN}/mcp`);
  assert.equal(r.status, 200);
  assert.ok(fs.existsSync(marker), "the first child really failed");
  assert.ok(lines.some((l) => /retrying/.test(l)), lines.join("\n"));
});

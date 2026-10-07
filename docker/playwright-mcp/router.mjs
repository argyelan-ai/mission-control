#!/usr/bin/env node
// playwright-mcp router (ADR-088 router step) — one playwright-mcp child per
// browser session, so everything a session opens goes over its own gateway
// address and belongs to it.
//
// Why: a single playwright-mcp serves every Claude agent as ONE unidentified
// client of the shared browser. playwright-mcp itself has a single
// --cdp-endpoint, so per-session addresses need one child each.
//
// Routes (port 8931):
//   /s/<token>/mcp   child on <gateway>/s/<token>/   (a browser session, ADR-088)
//   /a/<slug>/mcp    child on <gateway>/a/<slug>/    (an agent's address)
//   /mcp             the shared child on the legacy endpoint — unchanged for
//                    every existing MCP config (extend, don't replace)
//   GET /healthz     router alive (starts no child)
//   DELETE /_router/sessions/<token>   stop that session's child (MC's control
//                    path when a session ends; needs the token)
// --session-port adds a listener (the one published to the host for heads)
// that serves ONLY /s/<token>/mcp and /healthz.
// Everything else is 404. A child is started on first use, listens on
// 127.0.0.1 only, is stopped after `idleMs` without an open request (an open
// event stream counts as activity) and started again when it died. A session
// child is only started for a token the gateway knows (GET
// <gateway>/s/<token>/json/version != 404), so made-up tokens start nothing.
// The shared child is never stopped for idleness and does not count against
// `maxChildren`: every existing agent depends on it. Requests and responses
// (incl. event streams) are piped through unchanged; only the path loses its
// /s/<token> or /a/<slug> prefix. A response whose child dies mid-way is
// aborted, never left hanging.
//
// Tokens are credentials: they never appear in this process's log, and the
// children's own output is forwarded with every token replaced.
//
// Standard library only (http, child_process, net), so the image stays the
// pinned playwright-mcp plus this file.
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import http from "node:http";
import net from "node:net";
import { pathToFileURL } from "node:url";

const TOKEN_RE = /^[A-Za-z0-9_-]{32,128}$/;
const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;
const SHARED = "_shared";
const REDACTED = "<REDACTED>";

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

function isListening(port) {
  return new Promise((resolve) => {
    const sock = net.connect(port, "127.0.0.1");
    sock.once("connect", () => {
      sock.destroy();
      resolve(true);
    });
    sock.once("error", () => {
      sock.destroy();
      resolve(false);
    });
  });
}

// GET <gateway>/s/<token>/json/version: 404 = the gateway knows no such
// session. Anything else but 2xx is an error (the caller answers 502).
export async function gatewayKnowsSession(gatewayUrl, token, timeoutMs = 5000) {
  const res = await fetch(`${gatewayUrl.replace(/\/$/, "")}/s/${token}/json/version`, {
    signal: AbortSignal.timeout(timeoutMs),
  });
  await res.body?.cancel();
  if (res.status === 404) return false;
  if (res.ok) return true;
  throw new Error(`gateway answered ${res.status}`);
}

// `gaveUp()` true = the caller stopped waiting (the child exited): no more polling.
function waitForPort(port, deadline, gaveUp = () => false) {
  return new Promise((resolve, reject) => {
    const attempt = () => {
      if (gaveUp()) return reject(new Error("gave up"));
      const sock = net.connect(port, "127.0.0.1");
      sock.once("connect", () => {
        sock.destroy();
        resolve();
      });
      sock.once("error", () => {
        sock.destroy();
        if (Date.now() > deadline) reject(new Error("child did not start listening"));
        else setTimeout(attempt, 50);
      });
    };
    attempt();
  });
}

// "/s/<token>/mcp" -> { key, kind, id, rest: "/mcp" }; null = not a route.
export function parseRoute(url) {
  const [pathname, query = ""] = url.split("?");
  const suffix = query ? `?${query}` : "";
  if (pathname === "/mcp" || pathname.startsWith("/mcp/")) {
    return { kind: "shared", id: SHARED, rest: pathname + suffix };
  }
  const m = /^\/(s|a)\/([^/]+)(\/mcp(?:\/.*)?)$/.exec(pathname);
  if (!m) return null;
  const [, kind, id, rest] = m;
  if (kind === "s" && !TOKEN_RE.test(id)) return null;
  if (kind === "a" && !SLUG_RE.test(id)) return null;
  return { kind: kind === "s" ? "session" : "agent", id, rest: rest + suffix };
}

export function createRouter({
  gatewayUrl,
  legacyCdpEndpoint,
  childCommand = ["playwright-mcp"],
  childArgs = [],
  idleMs = 30 * 60 * 1000,
  maxChildren = 16,
  startTimeoutMs = 20000,
  startAttempts = 3,
  log = (line) => process.stderr.write(line + "\n"),
  allocatePort = freePort,
  checkSession = (token) => gatewayKnowsSession(gatewayUrl, token),
}) {
  const children = new Map(); // key -> { proc, port, ready, timer, label, active, open, live, stopped }
  const tokens = new Set(); // tokens of sessions with a child (or being checked), for redaction
  const usedPorts = new Set(); // ports our children hold or are about to bind
  const servers = [];

  const redact = (text) => {
    let out = String(text);
    for (const token of tokens) out = out.split(token).join(REDACTED);
    return out;
  };
  const say = (line) => log(redact(line));

  const keyOf = (route) => `${route.kind}:${route.id}`;
  const SHARED_KEY = `shared:${SHARED}`;
  const countedChildren = () => children.size - (children.has(SHARED_KEY) ? 1 : 0);
  const forgetToken = (route) => {
    if (route.kind === "session" && !children.has(keyOf(route))) tokens.delete(route.id);
  };
  // A session is named by a short hash of its token in the log, never the token.
  const labelOf = (route) =>
    route.kind === "session"
      ? `session #${createHash("sha256").update(route.id).digest("hex").slice(0, 8)}`
      : `${route.kind} ${route.id}`;
  const cdpOf = (route) => {
    if (route.kind === "shared") return legacyCdpEndpoint;
    const prefix = route.kind === "session" ? "s" : "a";
    return `${gatewayUrl.replace(/\/$/, "")}/${prefix}/${route.id}/`;
  };

  function stop(key, why) {
    const child = children.get(key);
    if (!child) return false;
    children.delete(key);
    clearTimeout(child.timer);
    child.stopped = true;
    child.proc?.kill("SIGTERM");
    // Open responses end with the child; a client must not wait on a stream
    // nobody will ever finish.
    for (const res of child.open) res.destroy();
    say(`router: stopped child for ${child.label} (${why})`);
    return true;
  }

  // The idle timer only runs while no request or stream is open; the shared
  // child has none at all.
  function arm(key, child) {
    clearTimeout(child.timer);
    child.timer = null;
    if (key === SHARED_KEY || child.active > 0 || children.get(key) !== child) return;
    child.timer = setTimeout(() => stop(key, "idle"), idleMs);
    child.timer.unref?.();
  }

  // A port no child of ours holds or is about to bind, and nobody listens on:
  // two children on one port would let one session's requests reach the
  // other session's browser.
  async function reservePort() {
    for (let i = 0; i < 20; i++) {
      const port = await allocatePort();
      // Checked again after the await: another start may have taken it meanwhile.
      if (usedPorts.has(port) || (await isListening(port)) || usedPorts.has(port)) continue;
      usedPorts.add(port);
      return port;
    }
    throw new Error("no free port for a child");
  }

  async function startOnce(key, route, child) {
    const port = await reservePort();
    if (child.stopped) {
      usedPorts.delete(port);
      throw new Error("stopped while starting");
    }
    const [cmd, ...pre] = childCommand;
    const args = [...pre, "--cdp-endpoint", cdpOf(route), "--port", String(port), "--host", "127.0.0.1", ...childArgs];
    const proc = spawn(cmd, args, { stdio: ["ignore", "pipe", "pipe"] });
    child.proc = proc;
    child.port = port;
    for (const stream of [proc.stdout, proc.stderr]) {
      stream.on("data", (chunk) => {
        for (const line of String(chunk).split("\n")) if (line.trim()) say(`[${child.label}] ${line}`);
      });
    }
    // The port stays reserved until the process (and its output) is gone.
    proc.on("close", () => {
      usedPorts.delete(port);
      forgetToken(route);
    });
    const ended = new Promise((_, reject) => {
      proc.once("error", (err) => {
        usedPorts.delete(port);
        reject(err);
      });
      proc.once("exit", (code, signal) => reject(new Error(`child exited while starting (${signal || code})`)));
    });
    proc.on("exit", (code, signal) => {
      if (child.live && child.proc === proc && children.get(key) === child) {
        children.delete(key);
        clearTimeout(child.timer);
        for (const res of child.open) res.destroy();
        say(`router: child for ${child.label} exited (${signal || code})`);
      }
    });
    proc.on("error", (err) => say(`router: child for ${child.label}: ${err.message}`));
    say(`router: started child for ${child.label}`);
    try {
      await Promise.race([waitForPort(port, Date.now() + startTimeoutMs, () => proc.exitCode !== null || proc.signalCode !== null), ended]);
      if (proc.exitCode !== null || proc.signalCode !== null) throw new Error("child exited while starting");
    } catch (err) {
      proc.kill("SIGTERM");
      throw err;
    }
    child.live = true;
  }

  async function spawnChild(key, route, child) {
    for (let attempt = 1; ; attempt++) {
      try {
        await startOnce(key, route, child);
        return;
      } catch (err) {
        if (child.stopped || attempt >= startAttempts) throw err;
        say(`router: start of ${child.label} failed (${err.message}), retrying`);
      }
    }
  }

  // The entry is registered BEFORE anything is awaited, so a burst of first
  // requests for one session shares one child instead of starting several.
  async function ensureChild(route) {
    const key = keyOf(route);
    let child = children.get(key);
    if (!child) {
      if (key !== SHARED_KEY && countedChildren() >= maxChildren) return null;
      child = {
        proc: null, port: null, label: labelOf(route), timer: null, ready: null,
        active: 0, open: new Set(), live: false, stopped: false,
      };
      children.set(key, child);
      child.ready = spawnChild(key, route, child);
      child.ready.catch(() => {
        if (children.get(key) === child) stop(key, "start failed");
        forgetToken(route);
      });
    }
    await child.ready;
    return child;
  }

  function proxy(req, res, key, child, rest) {
    child.active += 1;
    child.open.add(res);
    arm(key, child);
    let finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      child.open.delete(res);
      child.active -= 1;
      arm(key, child);
    };
    const upstream = http.request(
      { host: "127.0.0.1", port: child.port, method: req.method, path: rest, headers: { ...req.headers, host: `127.0.0.1:${child.port}` } },
      (up) => {
        res.writeHead(up.statusCode || 502, up.headers);
        up.pipe(res);
        // The child went away mid-response (crash, stop): abort the client's
        // response instead of leaving it open forever.
        up.on("error", () => res.destroy());
        up.on("close", () => {
          if (!up.complete) res.destroy();
        });
      },
    );
    upstream.on("error", (err) => {
      say(`router: upstream error for ${child.label}: ${err.message}`);
      if (res.headersSent) {
        res.destroy();
        return;
      }
      res.writeHead(502, { "content-type": "text/plain" });
      res.end("router: playwright-mcp unavailable");
    });
    req.pipe(upstream);
    res.on("close", () => {
      upstream.destroy();
      finish();
    });
  }

  // A session child only for a token the gateway knows; checked once per
  // child start, not per request.
  async function sessionKnown(route, res) {
    if (route.kind !== "session" || children.has(keyOf(route))) return true;
    tokens.add(route.id);
    try {
      if (await checkSession(route.id)) return true;
      res.writeHead(404, { "content-type": "text/plain" });
      res.end("router: unknown browser session");
    } catch (err) {
      say(`router: gateway check for ${labelOf(route)} failed: ${err.message}`);
      res.writeHead(502, { "content-type": "text/plain" });
      res.end("router: cdp-gateway unavailable");
    }
    forgetToken(route);
    return false;
  }

  // `sessionsOnly`: the listener published to the host (heads) serves only
  // /s/<token>/mcp and /healthz — no shared /mcp, no agent address, no control
  // path: from outside Docker, the browser needs a session's token.
  async function handle(req, res, sessionsOnly) {
    const url = req.url || "/";
    if (req.method === "GET" && url === "/healthz") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: true, children: children.size }));
      return;
    }
    const control = /^\/_router\/sessions\/([^/?]+)$/.exec(url);
    if (control && !sessionsOnly) {
      const token = control[1];
      if (req.method !== "DELETE" || !TOKEN_RE.test(token)) {
        res.writeHead(404).end();
        return;
      }
      const stopped = stop(`session:${token}`, "session ended");
      res.writeHead(stopped ? 200 : 404, { "content-type": "application/json" });
      res.end(JSON.stringify({ stopped }));
      return;
    }
    const route = parseRoute(url);
    if (!route || (sessionsOnly && route.kind !== "session")) {
      res.writeHead(404, { "content-type": "text/plain" });
      res.end("router: unknown route");
      return;
    }
    if (!(await sessionKnown(route, res))) return;
    let child;
    try {
      child = await ensureChild(route);
    } catch (err) {
      say(`router: could not start child for ${labelOf(route)}: ${err.message}`);
      res.writeHead(502, { "content-type": "text/plain" });
      res.end("router: playwright-mcp did not start");
      return;
    }
    if (!child) {
      forgetToken(route);
      res.writeHead(503, { "content-type": "text/plain" });
      res.end("router: too many browser sessions at once");
      return;
    }
    proxy(req, res, keyOf(route), child, route.rest);
  }

  return {
    // Resolves to the bound port. Call again with { sessionsOnly: true } for
    // the host-facing listener.
    listen(port, host, { sessionsOnly = false } = {}) {
      const server = http.createServer((req, res) => {
        handle(req, res, sessionsOnly).catch((err) => {
          say(`router: ${err.message}`);
          if (!res.headersSent) res.writeHead(500);
          res.end();
        });
      });
      servers.push(server);
      return new Promise((resolve) => server.listen(port, host, () => resolve(server.address().port)));
    },
    get port() {
      return servers[0]?.address()?.port;
    },
    childCount: () => children.size,
    tokenCount: () => tokens.size,
    async close() {
      for (const key of [...children.keys()]) stop(key, "router closing");
      await Promise.all(servers.map((server) => new Promise((resolve) => server.close(resolve))));
    },
  };
}

// CLI: router.mjs --gateway <url> --legacy-cdp-endpoint <url> [--listen 8931]
//                 [--idle-minutes 30] [--max-children 16] -- <playwright-mcp args>
// --max-children counts session and agent children; the shared one is extra.
const OWN_FLAGS = new Set([
  "--listen", "--host", "--session-port", "--gateway", "--legacy-cdp-endpoint", "--idle-minutes", "--max-children",
]);

export function parseCli(argv) {
  const sep = argv.indexOf("--");
  const own = sep >= 0 ? argv.slice(0, sep) : argv;
  const childArgs = sep >= 0 ? argv.slice(sep + 1) : [];
  // Every router flag takes a value; anything else is a mistake (e.g. a
  // playwright-mcp flag placed before `--`) and must not start a server.
  for (let i = 0; i < own.length; i += 2) {
    if (!OWN_FLAGS.has(own[i]) || own[i + 1] === undefined) {
      throw new Error(`unknown router flag: ${own[i]} (playwright-mcp flags go after --)`);
    }
  }
  const get = (name, fallback) => {
    const i = own.indexOf(name);
    return i >= 0 ? own[i + 1] : fallback;
  };
  return {
    listen: Number(get("--listen", "8931")),
    // Optional second listener that serves only /s/<token>/mcp (published to the host).
    sessionPort: get("--session-port") ? Number(get("--session-port")) : null,
    host: get("--host", "0.0.0.0"),
    gatewayUrl: get("--gateway", "http://cdp-browser:9300"),
    legacyCdpEndpoint: get("--legacy-cdp-endpoint", "http://cdp-browser:9223"),
    idleMs: Number(get("--idle-minutes", "30")) * 60 * 1000,
    maxChildren: Number(get("--max-children", "16")),
    childArgs,
  };
}

if (import.meta.url === pathToFileURL(process.argv[1] || "").href) {
  let cli;
  try {
    cli = parseCli(process.argv.slice(2));
  } catch (err) {
    process.stderr.write(`router: ${err.message}\n`);
    process.exit(2);
  }
  const router = createRouter(cli);
  await router.listen(cli.listen, cli.host);
  process.stderr.write(`router: listening on ${cli.host}:${cli.listen}\n`);
  if (cli.sessionPort) {
    await router.listen(cli.sessionPort, cli.host, { sessionsOnly: true });
    process.stderr.write(`router: session addresses only on ${cli.host}:${cli.sessionPort}\n`);
  }
  for (const sig of ["SIGTERM", "SIGINT"]) {
    process.on(sig, async () => {
      await router.close();
      process.exit(0);
    });
  }
}

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
// Everything else is 404. A child is started on first use, listens on
// 127.0.0.1 only, is stopped after `idleMs` without requests and started again
// when it died. Requests and responses (incl. event streams) are piped through
// unchanged; only the path loses its /s/<token> or /a/<slug> prefix.
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

function waitForPort(port, deadline) {
  return new Promise((resolve, reject) => {
    const attempt = () => {
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
  log = (line) => process.stderr.write(line + "\n"),
  allocatePort = freePort,
}) {
  const children = new Map(); // key -> { proc, port, ready, timer, label }
  const tokens = new Set(); // every token seen, for redaction
  let server;

  const redact = (text) => {
    let out = String(text);
    for (const token of tokens) out = out.split(token).join(REDACTED);
    return out;
  };
  const say = (line) => log(redact(line));

  const keyOf = (route) => `${route.kind}:${route.id}`;
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
    say(`router: stopped child for ${child.label} (${why})`);
    return true;
  }

  function touch(key) {
    const child = children.get(key);
    if (!child) return;
    clearTimeout(child.timer);
    child.timer = setTimeout(() => stop(key, "idle"), idleMs);
    child.timer.unref?.();
  }

  async function spawnChild(key, route, child) {
    child.port = await allocatePort();
    if (child.stopped) throw new Error("stopped while starting");
    const [cmd, ...pre] = childCommand;
    const args = [...pre, "--cdp-endpoint", cdpOf(route), "--port", String(child.port), "--host", "127.0.0.1", ...childArgs];
    const proc = spawn(cmd, args, { stdio: ["ignore", "pipe", "pipe"] });
    child.proc = proc;
    for (const stream of [proc.stdout, proc.stderr]) {
      stream.on("data", (chunk) => {
        for (const line of String(chunk).split("\n")) if (line.trim()) say(`[${child.label}] ${line}`);
      });
    }
    proc.on("exit", (code, signal) => {
      if (children.get(key) === child) {
        children.delete(key);
        clearTimeout(child.timer);
        say(`router: child for ${child.label} exited (${signal || code})`);
      }
    });
    say(`router: started child for ${child.label}`);
    await waitForPort(child.port, Date.now() + startTimeoutMs);
  }

  // The entry is registered BEFORE anything is awaited, so a burst of first
  // requests for one session shares one child instead of starting several.
  async function ensureChild(route) {
    const key = keyOf(route);
    let child = children.get(key);
    if (!child) {
      if (children.size >= maxChildren) return null;
      child = { proc: null, port: null, label: labelOf(route), timer: null, ready: null, stopped: false };
      children.set(key, child);
      child.ready = spawnChild(key, route, child);
      child.ready.catch(() => {
        if (children.get(key) === child) stop(key, "start failed");
      });
    }
    await child.ready;
    touch(key);
    return child;
  }

  function proxy(req, res, child, rest) {
    const upstream = http.request(
      { host: "127.0.0.1", port: child.port, method: req.method, path: rest, headers: { ...req.headers, host: `127.0.0.1:${child.port}` } },
      (up) => {
        res.writeHead(up.statusCode || 502, up.headers);
        up.pipe(res);
      },
    );
    upstream.on("error", (err) => {
      say(`router: upstream error for ${child.label}: ${err.message}`);
      if (!res.headersSent) res.writeHead(502, { "content-type": "text/plain" });
      res.end("router: playwright-mcp unavailable");
    });
    req.pipe(upstream);
    res.on("close", () => upstream.destroy());
  }

  async function handle(req, res) {
    const url = req.url || "/";
    if (req.method === "GET" && url === "/healthz") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: true, children: children.size }));
      return;
    }
    const control = /^\/_router\/sessions\/([^/?]+)$/.exec(url);
    if (control) {
      const token = control[1];
      if (req.method !== "DELETE" || !TOKEN_RE.test(token)) {
        res.writeHead(404).end();
        return;
      }
      tokens.add(token);
      const stopped = stop(`session:${token}`, "session ended");
      res.writeHead(stopped ? 200 : 404, { "content-type": "application/json" });
      res.end(JSON.stringify({ stopped }));
      return;
    }
    const route = parseRoute(url);
    if (!route) {
      res.writeHead(404, { "content-type": "text/plain" });
      res.end("router: unknown route");
      return;
    }
    if (route.kind === "session") tokens.add(route.id);
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
      res.writeHead(503, { "content-type": "text/plain" });
      res.end("router: too many browser sessions at once");
      return;
    }
    proxy(req, res, child, route.rest);
  }

  return {
    listen(port, host) {
      server = http.createServer((req, res) => {
        handle(req, res).catch((err) => {
          say(`router: ${err.message}`);
          if (!res.headersSent) res.writeHead(500);
          res.end();
        });
      });
      return new Promise((resolve) => server.listen(port, host, resolve));
    },
    get port() {
      return server?.address()?.port;
    },
    childCount: () => children.size,
    async close() {
      for (const key of [...children.keys()]) stop(key, "router closing");
      if (server) await new Promise((resolve) => server.close(resolve));
    },
  };
}

// CLI: router.mjs --gateway <url> --legacy-cdp-endpoint <url> [--listen 8931]
//                 [--idle-minutes 30] [--max-children 16] -- <playwright-mcp args>
function parseCli(argv) {
  const sep = argv.indexOf("--");
  const own = sep >= 0 ? argv.slice(0, sep) : argv;
  const childArgs = sep >= 0 ? argv.slice(sep + 1) : [];
  const get = (name, fallback) => {
    const i = own.indexOf(name);
    return i >= 0 ? own[i + 1] : fallback;
  };
  return {
    listen: Number(get("--listen", "8931")),
    host: get("--host", "0.0.0.0"),
    gatewayUrl: get("--gateway", "http://cdp-browser:9300"),
    legacyCdpEndpoint: get("--legacy-cdp-endpoint", "http://cdp-browser:9223"),
    idleMs: Number(get("--idle-minutes", "30")) * 60 * 1000,
    maxChildren: Number(get("--max-children", "16")),
    childArgs,
  };
}

if (import.meta.url === pathToFileURL(process.argv[1] || "").href) {
  const cli = parseCli(process.argv.slice(2));
  const router = createRouter(cli);
  await router.listen(cli.listen, cli.host);
  process.stderr.write(`router: listening on ${cli.host}:${cli.listen}\n`);
  for (const sig of ["SIGTERM", "SIGINT"]) {
    process.on(sig, async () => {
      await router.close();
      process.exit(0);
    });
  }
}

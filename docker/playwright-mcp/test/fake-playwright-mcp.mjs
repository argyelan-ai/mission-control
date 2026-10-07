#!/usr/bin/env node
// Stand-in for `playwright-mcp` in the router tests: same CLI shape for the
// flags the router sets, answers every request with what it was started with
// and what it received. Not shipped in the image.
import fs from "node:fs";
import http from "node:http";

const args = process.argv.slice(2);
const flag = (name) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : undefined;
};
const port = Number(flag("--port"));
const host = flag("--host") || "127.0.0.1";
const cdp = flag("--cdp-endpoint");
// Like the real server, it may mention its CDP endpoint in its own output.
process.stderr.write(`fake playwright-mcp connecting to ${cdp}\n`);
// --test-fail-once <file>: the first child started with it exits before it
// listens (a child that dies during start).
const failOnce = flag("--test-fail-once");
if (failOnce && !fs.existsSync(failOnce)) {
  fs.writeFileSync(failOnce, "failed once\n");
  process.exit(3);
}

http
  .createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      // An event stream that stays open (like MCP's standalone GET stream);
      // x-test-exit-after-ms: the child dies while it is open.
      if (req.headers["x-test-sse-hold"] === "1") {
        res.writeHead(200, { "content-type": "text/event-stream", "mcp-session-id": "child-session" });
        res.write(`data: ${process.pid}\n\n`);
        const exitAfter = Number(req.headers["x-test-exit-after-ms"] || 0);
        if (exitAfter) setTimeout(() => process.exit(1), exitAfter);
        return;
      }
      if (req.headers["x-test-sse"] === "1") {
        res.writeHead(200, { "content-type": "text/event-stream", "mcp-session-id": "child-session" });
        res.write("data: first\n\n");
        setTimeout(() => {
          res.write("data: second\n\n");
          res.end();
        }, 300);
        return;
      }
      res.writeHead(200, { "content-type": "application/json", "mcp-session-id": "child-session" });
      res.end(
        JSON.stringify({
          cdp,
          args,
          pid: process.pid,
          method: req.method,
          path: req.url,
          sessionHeader: req.headers["mcp-session-id"] || null,
          body: Buffer.concat(chunks).toString(),
        }),
      );
      if (req.headers["x-test-exit"] === "1") setTimeout(() => process.exit(1), 20);
    });
  })
  .listen(port, host);

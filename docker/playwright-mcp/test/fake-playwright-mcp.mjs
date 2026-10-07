#!/usr/bin/env node
// Stand-in for `playwright-mcp` in the router tests: same CLI shape for the
// flags the router sets, answers every request with what it was started with
// and what it received. Not shipped in the image.
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

http
  .createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
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

import { describe, expect, it } from "vitest";
import nextConfig from "./next.config";

// Bug (19.10.2026, fix-vorschlag.md Punkt 1): Next's built-in gzip compression
// buffers the SSE chat stream (/api/.../chat/stream) when it is proxied
// through the Next rewrite on :3000. Caddy already compresses responses
// (`encode gzip zstd` in the Caddyfile) for every other path, so Next must
// not double-compress and must not buffer event-stream responses.
describe("next.config.ts", () => {
  it("disables Next's response compression (Caddy already compresses; double compression buffers SSE)", () => {
    expect(nextConfig.compress).toBe(false);
  });
});

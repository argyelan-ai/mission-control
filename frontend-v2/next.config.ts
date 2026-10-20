import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

// UI i18n (cookie-based locale, see src/i18n/request.ts)
const withNextIntl = createNextIntlPlugin("./src/i18n/request.ts");

const nextConfig: NextConfig = {
  output: "standalone",
  typescript: {
    ignoreBuildErrors: true,
  },
  // Caddy already compresses every response (`encode gzip zstd` in the
  // Caddyfile) in front of this app. Next's own gzip compression buffers
  // text/event-stream responses (SSE) proxied through the /api rewrite
  // below — the chat stream over :3000 then sits for many seconds with no
  // bytes flushed, and the UI shows "status unknown" until the buffer fills
  // or the connection ends. Disabling it here fixes SSE for every route,
  // not just chat, and costs nothing because Caddy does the compression.
  // See ~/.mc/bench-chat/fix-vorschlag.md Punkt 1 for the live repro.
  compress: false,
  // API-Proxy: Browser spricht nur :3001 (same-origin), Next reicht /api/*
  // serverseitig ans Backend weiter. Nötig für Geräte-Tests (iPhone via
  // LAN/Tailscale) — dort zeigt "localhost:8000" sonst aufs Gerät selbst.
  // Greift nur, wenn NEXT_PUBLIC_API_URL leer ist (relative API-Calls).
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.API_PROXY_TARGET ?? "http://localhost:8000"}/api/:path*`,
      },
    ];
  },
  experimental: {
    serverActions: {
      allowedOrigins: [
        "localhost:3001",
        "localhost:80",
        "localhost",
        "mc.local",
        ...(process.env.PUBLIC_HOST ? [process.env.PUBLIC_HOST, `${process.env.PUBLIC_HOST}:80`] : []),
      ],
    },
  },
};

export default withNextIntl(nextConfig);

import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  typescript: {
    ignoreBuildErrors: true,
  },
  // API-Proxy: Browser spricht nur :3001 (same-origin), Next reicht /api/*
  // serverseitig ans Backend weiter. Nötig für Geräte-Tests (iPhone via
  // LAN/Tailscale) — dort zeigt "localhost:8000" sonst aufs Gerät selbst.
  // Greift nur, wenn NEXT_PUBLIC_API_URL leer ist (relative API-Calls).
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://localhost:8000/api/:path*",
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

export default nextConfig;

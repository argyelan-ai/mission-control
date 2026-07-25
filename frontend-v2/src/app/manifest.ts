import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: process.env.NEXT_PUBLIC_BRAND?.replace(".", "") || "Mission Control",
    short_name: process.env.NEXT_PUBLIC_BRAND?.replace(".", "") || "Mission Control",
    description: "AI Agent Command Center",
    start_url: "/",
    display: "standalone",
    theme_color: "#04070C",
    background_color: "#04070C",
    icons: [
      {
        src: "/icons/icon-192x192.png",
        sizes: "192x192",
        type: "image/png",
      },
      {
        src: "/icons/icon-512x512.png",
        sizes: "512x512",
        type: "image/png",
      },
    ],
  };
}

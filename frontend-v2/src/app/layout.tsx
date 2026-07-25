import type { Metadata, Viewport } from "next";
import "@/styles/globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: process.env.NEXT_PUBLIC_BRAND?.replace(".", "") || "Mission Control",
  description: "AI Agent Command Center",
  manifest: "/manifest.webmanifest",
  appleWebApp: {
    capable: true,
    statusBarStyle: "black-translucent",
    title: process.env.NEXT_PUBLIC_BRAND?.replace(".", "") || "Mission Control",
  },
  other: {
    "theme-color": "#04070C", // C.bgDeep
  },
};

export const viewport: Viewport = {
  themeColor: "#04070C", // C.bgDeep
  viewportFit: "cover",
  width: "device-width",
  initialScale: 1,
  // No maximumScale — pinch-zoom must stay enabled (WCAG 1.4.4 Resize Text).
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html
      lang="de"
      className="dark"
      style={{ colorScheme: "dark" }}
      suppressHydrationWarning
    >
      <head>
        <meta name="apple-mobile-web-app-capable" content="yes" />
        <link rel="icon" href="/favicon.ico" sizes="any" />
        {/* Preload the two critical self-hosted fonts (first paint: UI sans + display) */}
        <link rel="preload" href="/fonts/GeneralSans-400.woff2" as="font" type="font/woff2" crossOrigin="anonymous" />
        <link rel="preload" href="/fonts/ClashDisplay-600.woff2" as="font" type="font/woff2" crossOrigin="anonymous" />
      </head>
      <body className="font-sans antialiased bg-[var(--color-bg-deep)] text-[var(--color-text-primary)] min-h-[100dvh] overflow-x-hidden">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}

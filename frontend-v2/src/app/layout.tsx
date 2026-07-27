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
    "theme-color": "#0A0A0A", // C.bgDeep
  },
};

export const viewport: Viewport = {
  themeColor: "#0A0A0A", // C.bgDeep
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
        {/*
          --app-h: die echte Viewport-Höhe, selbst gemessen.

          iOS meldet die Viewport-Höhe beim ERSTEN Rendern zu klein und
          korrigiert sie erst, wenn ein Scroll-/Resize-Ereignis feuert. `dvh`
          erbt diesen Fehler, weil es denselben Wert benutzt — die App-Box endet
          dadurch oberhalb des Bildschirmrands, und die untere Leiste steht in
          der Luft. Sobald der Operator die Seite bewegt, springt sie an die
          richtige Stelle: genau die Signatur dieses Bugs.

          Deshalb wird die Höhe hier gesetzt, bevor React hydriert (kein
          sichtbarer Sprung), und danach bei jedem Ereignis nachgezogen, das die
          Korrektur auslösen könnte. Die verzögerten Messungen fangen den Fall
          ab, dass iOS den richtigen Wert erst nach dem ersten Frame liefert,
          ohne ein Event zu feuern.

          innerHeight (Layout-Viewport), nicht visualViewport.height: letzteres
          schrumpft bei geöffneter Tastatur, was die Leiste über die Tastatur
          ziehen würde.
        */}
        <script
          dangerouslySetInnerHTML={{
            __html:
              "(function(){var d=document.documentElement;" +
              "function m(){d.style.setProperty('--app-h',window.innerHeight+'px')}" +
              "m();" +
              "addEventListener('resize',m);addEventListener('orientationchange',m);" +
              "addEventListener('pageshow',m);" +
              "if(window.visualViewport){visualViewport.addEventListener('resize',m)}" +
              "requestAnimationFrame(m);setTimeout(m,150);setTimeout(m,600);" +
              "})()",
          }}
        />
      </head>
      <body className="font-sans antialiased bg-[var(--color-bg-deep)] text-[var(--color-text-primary)] min-h-[100dvh] overflow-x-hidden">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}

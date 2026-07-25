"use client";

/**
 * AmbientBackground — v3.1
 *
 * Ruhige, dunkle Bühne: ein dezenter Cyan-Schleier oben links für Tiefe,
 * dazu feine Grain-Textur gegen digitalen Flach-Look. Kein Raster, keine
 * Muster — der Fokus gehört dem Inhalt. Rein dekorativ (aria-hidden).
 */
export function AmbientBackground() {
  return (
    <div
      aria-hidden
      className="pointer-events-none fixed inset-0 z-0 overflow-hidden"
    >
      {/* Ein einziger, ruhiger Cyan-Schleier — Tiefe ohne Glow */}
      <div
        className="absolute inset-0"
        style={{
          background:
            "radial-gradient(ellipse 60% 45% at 18% -5%, rgba(0,229,255,0.05) 0%, rgba(0,229,255,0) 70%)",
        }}
      />

      {/* Grain overlay */}
      <div
        className="fixed inset-0 opacity-[0.03]"
        style={{
          backgroundImage: `url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")`,
          backgroundRepeat: "repeat",
          backgroundSize: "128px 128px",
        }}
      />
    </div>
  );
}

"use client";

/**
 * AmbientBackground — v3.1
 *
 * Ruhige, dunkle Bühne: ein dezenter Helligkeits-Schleier oben links für Tiefe,
 * dazu feine Grain-Textur gegen digitalen Flach-Look. Kein Raster, keine
 * Muster — der Fokus gehört dem Inhalt. Rein dekorativ (aria-hidden).
 *
 * System A: der frühere Helligkeits-Schleier war reine Dekor-Buntheit und damit ein
 * Fremdkörper — Farbe bedeutet im Cockpit nur noch Status. Die Tiefenwirkung
 * bleibt, sie läuft jetzt über Helligkeit (achromatisches Weiss) statt Farbton.
 */
export function AmbientBackground() {
  return (
    <div
      aria-hidden
      className="pointer-events-none fixed inset-0 z-0 overflow-hidden"
    >
      {/* Ein einziger, ruhiger Helligkeits-Schleier — Tiefe ohne Farbe, ohne Glow */}
      <div
        className="absolute inset-0"
        style={{
          background:
            "radial-gradient(ellipse 60% 45% at 18% -5%, rgba(255,255,255,0.035) 0%, rgba(255,255,255,0) 70%)",
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

"use client";

/**
 * TEMPORÄR — Einblendung der echten Viewport-Werte auf dem Gerät.
 *
 * Die untere Leiste sitzt auf dem iPhone zu hoch und rutscht erst nach einer
 * Scroll-Geste an ihren Platz. Zwei Fixes ins Blaue haben nichts geändert, und
 * Chrome reproduziert den Fehler nicht — es braucht die echten Zahlen.
 *
 * Eine eigene Route (/diag) hilft nicht: In einer installierten PWA gibt es
 * keine Adressleiste, dorthin kann der Operator gar nicht navigieren. Deshalb
 * blendet sich die Messung hier in die App selbst ein.
 *
 * Nur mobil, nur zum Ablesen. Nach der Diagnose ersatzlos entfernen.
 */

import { useEffect, useState } from "react";

export default function ViewportDebug() {
  const [txt, setTxt] = useState<string[]>([]);

  useEffect(() => {
    const measure = () => {
      const de = document.documentElement;
      const p = document.createElement("div");
      p.style.cssText =
        "position:absolute;visibility:hidden;top:0;left:0;width:1px";
      document.body.appendChild(p);
      const h = (unit: string) => {
        p.style.height = unit;
        return Math.round(p.getBoundingClientRect().height);
      };
      const dvh = h("100dvh");
      const svh = h("100svh");
      const lvh = h("100lvh");
      const vh = h("100vh");
      p.remove();

      const cs = getComputedStyle(de);
      const sab = cs.getPropertyValue("--probe-sab").trim() || "0px";
      const appH = cs.getPropertyValue("--app-h").trim() || "—";

      // Der eigentliche Messwert: Abstand zwischen Unterkante der Tab-Leiste
      // und Unterkante des Fensters. Muss 0 sein.
      const bar = document.querySelector("nav[aria-label='Hauptnavigation']");
      const gap = bar
        ? Math.round(window.innerHeight - bar.getBoundingClientRect().bottom)
        : NaN;

      setTxt([
        `GAP ${Number.isNaN(gap) ? "?" : gap}`,
        `inner ${window.innerHeight}`,
        `vv ${Math.round(window.visualViewport?.height ?? 0)}`,
        `dvh ${dvh}  svh ${svh}`,
        `lvh ${lvh}  vh ${vh}`,
        `screen ${window.screen.height}`,
        `sab ${sab}  appH ${appH}`,
      ]);
    };

    measure();
    const t1 = setTimeout(measure, 300);
    const t2 = setTimeout(measure, 1200);
    window.addEventListener("resize", measure);
    window.visualViewport?.addEventListener("resize", measure);
    return () => {
      clearTimeout(t1);
      clearTimeout(t2);
      window.removeEventListener("resize", measure);
      window.visualViewport?.removeEventListener("resize", measure);
    };
  }, []);

  return (
    <>
      <style>{`:root{--probe-sab:env(safe-area-inset-bottom)}`}</style>
      <div
        className="md:hidden"
        style={{
          position: "fixed",
          top: "calc(env(safe-area-inset-top) + 4rem)",
          left: 8,
          zIndex: 90,
          padding: "6px 8px",
          borderRadius: 4,
          background: "rgba(0,0,0,0.85)",
          border: "1px solid #FA4942",
          color: "#EEEEEE",
          font: "600 11px/1.5 ui-monospace, monospace",
          pointerEvents: "none",
          whiteSpace: "pre",
        }}
      >
        {txt.join("\n")}
      </div>
    </>
  );
}

"use client";

/**
 * /diag — temporäre Messseite für das iOS-Layout-Problem der Bottom-Leiste.
 *
 * Bewusst OHNE AppShell, ohne API-Aufrufe und ohne Auth: sie soll auf dem
 * Gerät des Operators laufen, ohne dass irgendwer sich anmelden muss. Zwei
 * Hypothesen (Blaustich der Neutralen, dann `position: fixed`) haben sich als
 * falsch erwiesen — statt einer dritten Vermutung misst diese Seite, was iOS
 * tatsächlich tut, und zeigt es als nackte Zahlen.
 *
 * Nach der Diagnose wieder entfernen.
 */

import { useEffect, useState } from "react";

type Row = { k: string; v: string; warn?: boolean };

function px(n: number | undefined) {
  return n === undefined ? "—" : `${Math.round(n * 100) / 100}px`;
}

export default function DiagPage() {
  const [rows, setRows] = useState<Row[]>([]);

  useEffect(() => {
    const measure = () => {
      const de = document.documentElement;
      const probe = document.createElement("div");
      probe.style.cssText =
        "position:absolute;visibility:hidden;height:100dvh;top:0;left:0;width:1px";
      document.body.appendChild(probe);
      const dvh = probe.getBoundingClientRect().height;
      probe.style.height = "100svh";
      const svh = probe.getBoundingClientRect().height;
      probe.style.height = "100lvh";
      const lvh = probe.getBoundingClientRect().height;
      probe.style.height = "100vh";
      const vh = probe.getBoundingClientRect().height;
      probe.remove();

      const cs = getComputedStyle(de);
      const insetBottom = cs.getPropertyValue("--probe-sab").trim();
      const insetTop = cs.getPropertyValue("--probe-sat").trim();

      const bar = document.getElementById("diag-bar");
      const barRect = bar?.getBoundingClientRect();

      const vv = window.visualViewport;
      const standalone =
        window.matchMedia("(display-mode: standalone)").matches ||
        // iOS-Legacy-Flag
        (window.navigator as unknown as { standalone?: boolean }).standalone === true;

      // DAS ist die entscheidende Zahl: wie viel Platz bleibt unter der Leiste?
      const gap = barRect ? window.innerHeight - barRect.bottom : undefined;

      setRows([
        { k: "GAP unter der Leiste", v: px(gap), warn: !!gap && Math.abs(gap) > 1 },
        { k: "Leiste bottom", v: px(barRect?.bottom) },
        { k: "Leiste height", v: px(barRect?.height) },
        { k: "window.innerHeight", v: px(window.innerHeight) },
        { k: "visualViewport.height", v: px(vv?.height) },
        { k: "visualViewport.offsetTop", v: px(vv?.offsetTop) },
        { k: "documentElement.clientHeight", v: px(de.clientHeight) },
        { k: "100dvh", v: px(dvh) },
        { k: "100svh", v: px(svh) },
        { k: "100lvh", v: px(lvh) },
        { k: "100vh", v: px(vh) },
        { k: "safe-area-inset-top", v: insetTop || "0px" },
        { k: "safe-area-inset-bottom", v: insetBottom || "0px" },
        { k: "screen.height", v: px(window.screen.height) },
        { k: "devicePixelRatio", v: String(window.devicePixelRatio) },
        { k: "display-mode standalone", v: standalone ? "JA (PWA)" : "nein (Browser)" },
      ]);
    };

    measure();
    window.addEventListener("resize", measure);
    window.visualViewport?.addEventListener("resize", measure);
    window.visualViewport?.addEventListener("scroll", measure);
    return () => {
      window.removeEventListener("resize", measure);
      window.visualViewport?.removeEventListener("resize", measure);
      window.visualViewport?.removeEventListener("scroll", measure);
    };
  }, []);

  return (
    <>
      {/* Die env()-Werte lassen sich nur über Custom Properties auslesen. */}
      <style>{`:root{--probe-sat:env(safe-area-inset-top);--probe-sab:env(safe-area-inset-bottom)}`}</style>

      {/* Exakt der Aufbau der App-Shell: h-dvh Flex-Spalte, Leiste als letztes
          Flex-Kind. Wenn die Leiste HIER nicht unten sitzt, liegt es an der
          Box — nicht an der Komponente. */}
      <div
        className="flex flex-col overflow-hidden"
        style={{
          height: "var(--app-h, 100dvh)",
          backgroundColor: "#0A0A0A",
          color: "#EEEEEE",
        }}
      >
        <div className="flex-1 overflow-y-auto" style={{ padding: "12px" }}>
          <div
            style={{
              fontFamily: "ui-monospace, monospace",
              fontSize: 13,
              lineHeight: 1.9,
            }}
          >
            <div style={{ fontSize: 15, fontWeight: 700, marginBottom: 10 }}>
              LAYOUT-MESSUNG
            </div>
            {rows.map((r) => (
              <div
                key={r.k}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  gap: 10,
                  borderBottom: "1px solid rgba(168,168,168,0.12)",
                  color: r.warn ? "#FA4942" : undefined,
                  fontWeight: r.warn ? 700 : undefined,
                }}
              >
                <span style={{ opacity: 0.75 }}>{r.k}</span>
                <span>{r.v}</span>
              </div>
            ))}
            <div style={{ marginTop: 14, opacity: 0.6, fontSize: 12 }}>
              GAP muss 0 sein. Ist er grösser, endet die Box über dem
              Bildschirmrand — dann liegt es nicht an der Leiste.
            </div>
          </div>
        </div>

        <div
          id="diag-bar"
          className="shrink-0"
          style={{
            backgroundColor: "#2C2C2C",
            borderTop: "1px solid rgba(168,168,168,0.16)",
            paddingBottom: "env(safe-area-inset-bottom)",
            minHeight: 52,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontFamily: "ui-monospace, monospace",
            fontSize: 12,
            letterSpacing: "0.08em",
          }}
        >
          TESTLEISTE — MUSS BÜNDIG UNTEN SITZEN
        </div>
      </div>
    </>
  );
}

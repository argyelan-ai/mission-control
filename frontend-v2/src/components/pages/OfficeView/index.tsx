"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ZoomIn, ZoomOut, Maximize2 } from "lucide-react";
import AppShell from "@/components/layout/AppShell";
import { OrgChart } from "./OrgChart";
import { C } from "@/lib/colors";

/**
 * Office page — Organigramm-only view (3D Office tab removed 2026-05-19).
 *
 * Zoom mechanics use CSS `zoom` instead of `transform: scale`:
 *   - actual layout reflow (vs transform's visual-only scaling)
 *   - ResizeObserver fires on dimension changes → OrgChart's SVG
 *     connector paths recompute correctly (the bug the operator hit where
 *     lines "hingen einfach so" when zooming)
 *   - getBoundingClientRect returns the scaled coordinates so all
 *     pixel-math in children stays consistent
 *
 * Browser support: Chromium-based + Safari + Firefox 126+ — fine for
 * the operator's Mac Mini.
 */

const ZOOM_MIN = 0.5;
const ZOOM_MAX = 1.8;
const ZOOM_STEP = 0.1;
// Default 0.85 so the full hierarchy fits on screen at standard
// viewport without scrolling — the operator's UX request.
const ZOOM_DEFAULT = 0.85;

function clampZoom(v: number): number {
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(v * 10) / 10));
}

export default function OfficeView() {
  const [zoom, setZoom] = useState<number>(ZOOM_DEFAULT);
  const panelRef = useRef<HTMLDivElement>(null);

  const zoomIn = useCallback(() => setZoom((z) => clampZoom(z + ZOOM_STEP)), []);
  const zoomOut = useCallback(() => setZoom((z) => clampZoom(z - ZOOM_STEP)), []);
  const zoomReset = useCallback(() => setZoom(ZOOM_DEFAULT), []);

  // Keyboard: ⌘/Ctrl + / − / 0
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      const tag = (document.activeElement?.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea") return;
      if (e.key === "+" || e.key === "=") { e.preventDefault(); zoomIn(); }
      else if (e.key === "-") { e.preventDefault(); zoomOut(); }
      else if (e.key === "0") { e.preventDefault(); zoomReset(); }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [zoomIn, zoomOut, zoomReset]);

  // Wheel-zoom inside the panel (Cmd/Ctrl held, or trackpad-pinch which the
  // browser surfaces as ctrlKey wheel events). Without modifier the wheel
  // scrolls the panel as normal.
  useEffect(() => {
    const el = panelRef.current;
    if (!el) return;
    const handler = (e: WheelEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      e.preventDefault();
      // deltaY < 0 = scroll up = zoom in
      const direction = e.deltaY < 0 ? 1 : -1;
      // Trackpad pinches fire many small events — scale by magnitude but
      // clamp to one step so a single pinch tick doesn't fly out of range.
      const magnitude = Math.min(1, Math.abs(e.deltaY) / 40);
      setZoom((z) => clampZoom(z + direction * ZOOM_STEP * magnitude));
    };
    el.addEventListener("wheel", handler, { passive: false });
    return () => el.removeEventListener("wheel", handler);
  }, []);

  return (
    <AppShell fullHeight>
      <div className="flex flex-col flex-1 overflow-hidden">
        {/* Header — title + zoom cluster (subtitle removed per operator's req) */}
        <header className="shrink-0 flex items-center justify-between gap-4 px-5 lg:px-7 pt-5 pb-4">
          <h1 className="text-[26px] font-bold text-white tracking-tight leading-tight">
            Organigramm
          </h1>
          <ZoomCluster zoom={zoom} onIn={zoomIn} onOut={zoomOut} onReset={zoomReset} />
        </header>

        {/* Outer scroll container — overflow-auto handles content taller
            than viewport at zoom > 1. The glass-surface lives on the INNER
            wrapper so its visual extent matches the content (not just the
            viewport rectangle) — the operator hit "hintergrund zu kurz" before. */}
        <div
          ref={panelRef}
          className="relative flex-1 min-h-0 overflow-auto" tabIndex={0} role="region" aria-label="Organigramm"
        >
          {/* Glass-panel — min-h-full so it always covers viewport even when
              content is short; intrinsic height grows with the chart when
              zoomed in. zoom (CSS prop) reflows layout — ResizeObserver in
              OrgChart picks up the new sizes and recomputes connectors. */}
          <div
            className="relative min-h-full"
            style={{
              zoom: zoom,
              background: C.bgBase,
              transition: "zoom 180ms ease",
            }}
          >
            {/* subtle grain — breaks digital flatness */}
            <div
              aria-hidden
              className="pointer-events-none absolute inset-0 opacity-[0.04] mix-blend-overlay"
              style={{
                backgroundImage:
                  "url(\"data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E\")",
              }}
            />
            <OrgChart zoom={zoom} />
          </div>
        </div>
      </div>
    </AppShell>
  );
}

// ── Zoom cluster ────────────────────────────────────────────────────────────

function ZoomCluster({
  zoom,
  onIn,
  onOut,
  onReset,
}: {
  zoom: number;
  onIn: () => void;
  onOut: () => void;
  onReset: () => void;
}) {
  const pct = Math.round(zoom * 100);
  const atMax = zoom >= ZOOM_MAX - 0.001;
  const atMin = zoom <= ZOOM_MIN + 0.001;

  return (
    <div
      className="flex items-center gap-px rounded-xl p-1 shrink-0"
      style={{
        background: C.bgElevated,
        border: `1px solid ${C.border}`,
      }}
    >
      <ZoomButton
        onClick={onOut}
        disabled={atMin}
        title="Auszoomen (⌘−)"
        aria-label="Zoom out"
      >
        <ZoomOut size={15} strokeWidth={2} />
      </ZoomButton>

      <button
        type="button"
        onClick={onReset}
        title="Auf 100% zurücksetzen (⌘0)"
        className="px-2.5 min-w-[52px] text-[11.5px] font-mono tabular-nums rounded-md transition-colors cursor-pointer"
        style={{
          color: zoom === ZOOM_DEFAULT ? C.textMuted : C.textSecondary,
          background: "transparent",
          border: "none",
          padding: "6px 10px",
        }}
        onMouseEnter={(e) => ((e.currentTarget as HTMLButtonElement).style.background = "rgba(255,255,255,0.04)")}
        onMouseLeave={(e) => ((e.currentTarget as HTMLButtonElement).style.background = "transparent")}
      >
        {pct}%
      </button>

      <ZoomButton
        onClick={onIn}
        disabled={atMax}
        title="Zoomen (⌘+)"
        aria-label="Zoom in"
      >
        <ZoomIn size={15} strokeWidth={2} />
      </ZoomButton>

      <span className="w-px h-5 mx-0.5" style={{ background: "rgba(255,255,255,0.06)" }} />

      <ZoomButton
        onClick={onReset}
        title="Anpassen / 100% (⌘0)"
        aria-label="Reset zoom"
      >
        <Maximize2 size={14} strokeWidth={2} />
      </ZoomButton>
    </div>
  );
}

function ZoomButton({
  children,
  disabled,
  onClick,
  title,
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className="grid place-items-center rounded-md cursor-pointer transition-colors"
      style={{
        width: 28,
        height: 28,
        background: "transparent",
        border: "none",
        color: disabled ? "rgba(255,255,255,0.18)" : "var(--color-text-secondary)",
        cursor: disabled ? "default" : "pointer",
      }}
      onMouseEnter={(e) => {
        if (!disabled) (e.currentTarget as HTMLButtonElement).style.background = "rgba(255,255,255,0.06)";
      }}
      onMouseLeave={(e) => {
        (e.currentTarget as HTMLButtonElement).style.background = "transparent";
      }}
      {...rest}
    >
      {children}
    </button>
  );
}

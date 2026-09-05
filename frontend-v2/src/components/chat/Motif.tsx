"use client";

/**
 * Motif — das gezeichnete Herkunfts-Zeichen einer Ereignis-Karte.
 *
 * Kein Icon, kein Emoji: vier kleine Canvas-Motive in MC-Blau, je eines pro
 * Herkunft (Abnahme durch den Operator am 04.09.2026, Artefakt „Motive"):
 *
 *   woge  — fluessiger Ring          → Auftrag
 *   strom — drei steigende Datenspalten → Hinweis / zugestellte Nachricht
 *   iris  — gegenlaeufige HUD-Boegen  → Rueckmeldung eines Teamkollegen
 *   kern  — Kern mit zwei Bahnen      → System
 *
 * Bewegung nur, solange das Ereignis „lebt" (der Agent arbeitet gerade daran);
 * danach steht das Motiv still und etwas gedimmter — Blau bleibt. Unter
 * `prefers-reduced-motion` laeuft nie eine Schleife: dann ist die ruhige
 * Fassung die einzige.
 */
import { useEffect, useRef } from "react";

import type { MessageSourceKind } from "@/lib/chatTypes";

export type MotifKind = "woge" | "strom" | "iris" | "kern";

export function motifForSource(kind: MessageSourceKind): MotifKind {
  switch (kind) {
    case "task":
      return "woge";
    case "nudge":
    case "inbox":
      return "strom";
    case "teammate":
      return "iris";
    case "system":
      return "kern";
  }
}

const BLUE = "#5890CA";
const LIGHT = "#8FC1FF";
const DEEP = "#2E5C8F";
const WHITE = "#E6F1FF";

type Ctx = CanvasRenderingContext2D;

function glow(ctx: Ctx, w: number, on: boolean) {
  ctx.shadowColor = `rgba(88,144,202,${on ? 0.9 : 0.35})`;
  ctx.shadowBlur = w * 0.1;
}

function woge(ctx: Ctx, w: number, t: number, on: boolean) {
  const cx = w / 2;
  const cy = w / 2;
  const R = w * 0.34;
  const th = w * 0.11;
  const n = 72;
  const amp = on ? 0.12 : 0.04;
  const ph = on ? t / 700 : 2.1;
  const edge = (r: number, d: number, a: number) =>
    r * (1 + amp * Math.sin(3 * a + d * ph) + amp * 0.5 * Math.sin(5 * a - d * ph * 1.4));
  ctx.beginPath();
  for (let i = 0; i <= n; i++) {
    const a = (i / n) * Math.PI * 2;
    const rr = edge(R + th / 2, 1, a);
    if (i) ctx.lineTo(cx + Math.cos(a) * rr, cy + Math.sin(a) * rr);
    else ctx.moveTo(cx + Math.cos(a) * rr, cy + Math.sin(a) * rr);
  }
  ctx.closePath();
  for (let j = n; j >= 0; j--) {
    const b = (j / n) * Math.PI * 2;
    const ri = edge(R - th / 2, -1, b);
    ctx.lineTo(cx + Math.cos(b) * ri, cy + Math.sin(b) * ri);
  }
  ctx.closePath();
  const g = ctx.createLinearGradient(cx - R, cy - R, cx + R, cy + R);
  const k = on ? (Math.sin(t / 900) + 1) / 2 : 0.5;
  g.addColorStop(0, LIGHT);
  g.addColorStop(0.5 + k * 0.3, BLUE);
  g.addColorStop(1, DEEP);
  glow(ctx, w, on);
  ctx.fillStyle = g;
  ctx.fill("evenodd");
  ctx.shadowBlur = 0;
  if (on) {
    const la = t / 1100;
    const lx = cx + Math.cos(la) * R;
    const ly = cy + Math.sin(la) * R;
    const lg = ctx.createRadialGradient(lx, ly, 0, lx, ly, th * 1.1);
    lg.addColorStop(0, "rgba(230,241,255,.95)");
    lg.addColorStop(1, "rgba(230,241,255,0)");
    ctx.fillStyle = lg;
    ctx.beginPath();
    ctx.arc(lx, ly, th * 1.1, 0, Math.PI * 2);
    ctx.fill();
  }
}

function strom(ctx: Ctx, w: number, t: number, on: boolean) {
  const cols = [
    { x: 0.28, sp: 1 / 1100, off: 0.0 },
    { x: 0.5, sp: 1 / 800, off: 0.45 },
    { x: 0.72, sp: 1 / 1400, off: 0.8 },
  ];
  const top = w * 0.14;
  const bot = w * 0.86;
  const H = bot - top;
  for (const c of cols) {
    const x = w * c.x;
    ctx.strokeStyle = "rgba(88,144,202,.22)";
    ctx.lineWidth = Math.max(1, w * 0.012);
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, bot);
    ctx.stroke();
    const u = on ? (t * c.sp + c.off) % 1 : c.off;
    for (let k = 7; k >= 0; k--) {
      let uu = u - k * 0.07;
      const s = 1 - k / 8;
      if (uu < 0) uu += 1;
      const y = bot - uu * H;
      ctx.fillStyle = `rgba(143,193,255,${on ? s * s : k ? 0 : 0.85})`;
      if (!k) glow(ctx, w, on);
      ctx.beginPath();
      ctx.arc(x, y, w * 0.05 * (0.35 + 0.65 * s), 0, Math.PI * 2);
      ctx.fill();
      ctx.shadowBlur = 0;
    }
  }
}

function iris(ctx: Ctx, w: number, t: number, on: boolean) {
  const cx = w / 2;
  const cy = w / 2;
  const rings = [
    { r: 0.42, seg: 3, gap: 0.55, sp: 1 / 1600, lw: 0.05 },
    { r: 0.28, seg: 2, gap: 0.9, sp: -1 / 1100, lw: 0.06 },
  ];
  rings.forEach((rg, i) => {
    const base = on ? t * rg.sp : 0.4 * i;
    const step = (Math.PI * 2) / rg.seg;
    for (let k = 0; k < rg.seg; k++) {
      const a0 = base + k * step;
      const a1 = a0 + step - rg.gap;
      const g = ctx.createLinearGradient(
        cx + Math.cos(a0) * w * rg.r,
        cy + Math.sin(a0) * w * rg.r,
        cx + Math.cos(a1) * w * rg.r,
        cy + Math.sin(a1) * w * rg.r,
      );
      g.addColorStop(0, `rgba(46,92,143,${on ? 0.55 : 0.4})`);
      g.addColorStop(1, LIGHT);
      ctx.strokeStyle = g;
      ctx.lineWidth = w * rg.lw;
      ctx.lineCap = "round";
      glow(ctx, w, on);
      ctx.beginPath();
      ctx.arc(cx, cy, w * rg.r, a0, a1);
      ctx.stroke();
      ctx.shadowBlur = 0;
    }
  });
  const pulse = on ? 0.85 + 0.15 * Math.sin(t / 450) : 0.9;
  const rgd = ctx.createRadialGradient(cx, cy, 0, cx, cy, w * 0.12 * pulse);
  rgd.addColorStop(0, WHITE);
  rgd.addColorStop(0.45, LIGHT);
  rgd.addColorStop(1, "rgba(88,144,202,0)");
  ctx.fillStyle = rgd;
  ctx.beginPath();
  ctx.arc(cx, cy, w * 0.12 * pulse, 0, Math.PI * 2);
  ctx.fill();
}

function kern(ctx: Ctx, w: number, t: number, on: boolean) {
  const cx = w / 2;
  const cy = w / 2;
  const R = w * 0.4;
  const orbits = [
    { tilt: 0.5, rot: 0.6, sp: 1 / 900, off: 0 },
    { tilt: -0.55, rot: -0.5, sp: 1 / 1300, off: 2.1 },
  ];
  for (const o of orbits) {
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(o.rot);
    ctx.strokeStyle = "rgba(88,144,202,.35)";
    ctx.lineWidth = Math.max(1, w * 0.012);
    ctx.beginPath();
    ctx.ellipse(0, 0, R, R * Math.abs(o.tilt), 0, 0, Math.PI * 2);
    ctx.stroke();
    const a = on ? t * o.sp + o.off : o.off;
    for (let k = 8; k >= 0; k--) {
      const aa = a - k * 0.09;
      const px = Math.cos(aa) * R;
      const py = Math.sin(aa) * R * Math.abs(o.tilt);
      const s = 1 - k / 9;
      ctx.fillStyle = `rgba(143,193,255,${on ? s * s : k ? 0 : 0.9})`;
      if (!k) glow(ctx, w, on);
      ctx.beginPath();
      ctx.arc(px, py, w * 0.035 * (0.4 + 0.6 * s), 0, Math.PI * 2);
      ctx.fill();
      ctx.shadowBlur = 0;
    }
    ctx.restore();
  }
  const pulse = on ? 0.9 + 0.1 * Math.sin(t / 500) : 0.95;
  const rg = ctx.createRadialGradient(cx, cy, 0, cx, cy, w * 0.16 * pulse);
  rg.addColorStop(0, WHITE);
  rg.addColorStop(0.35, LIGHT);
  rg.addColorStop(1, "rgba(88,144,202,0)");
  ctx.fillStyle = rg;
  ctx.beginPath();
  ctx.arc(cx, cy, w * 0.16 * pulse, 0, Math.PI * 2);
  ctx.fill();
}

const DRAW: Record<MotifKind, (ctx: Ctx, w: number, t: number, on: boolean) => void> = {
  woge,
  strom,
  iris,
  kern,
};

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
    : false;
}

interface MotifProps {
  kind: MotifKind;
  /** Bewegt sich nur, solange das Ereignis lebt. */
  live: boolean;
  /** CSS-Kantenlaenge in px; gezeichnet wird in Geraete-Pixeln. */
  size: number;
  className?: string;
}

export function Motif({ kind, live, size, className }: MotifProps) {
  const ref = useRef<HTMLCanvasElement>(null);
  // Einmal beim Einhaengen gelesen — wer die Einstellung umschaltet, laedt
  // ohnehin neu; ein Listener pro Karte waere mehr Maschinerie als Nutzen.
  const reduced = useRef<boolean | null>(null);
  if (reduced.current === null) reduced.current = prefersReducedMotion();
  const on = live && !reduced.current;

  useEffect(() => {
    const cvs = ref.current;
    if (!cvs) return;
    const ctx = cvs.getContext("2d");
    // jsdom (und ein Browser ohne 2D-Kontext) liefert null: dann bleibt die
    // Flaeche leer, die Karte funktioniert trotzdem.
    if (!ctx) return;
    const dpr = typeof window !== "undefined" ? Math.min(window.devicePixelRatio || 1, 3) : 1;
    const w = Math.round(size * dpr);
    cvs.width = w;
    cvs.height = w;

    const paint = (t: number) => {
      ctx.clearRect(0, 0, w, w);
      ctx.globalAlpha = on ? 1 : 0.7;
      DRAW[kind](ctx, w, t, on);
      ctx.globalAlpha = 1;
    };

    if (!on) {
      paint(0);
      return;
    }
    let frame = 0;
    const loop = (t: number) => {
      paint(t);
      frame = window.requestAnimationFrame(loop);
    };
    frame = window.requestAnimationFrame(loop);
    return () => window.cancelAnimationFrame(frame);
  }, [kind, on, size]);

  return (
    <canvas
      ref={ref}
      data-testid="motif"
      data-kind={kind}
      data-live={on}
      aria-hidden="true"
      className={className}
      style={{ width: size, height: size, display: "block" }}
    />
  );
}

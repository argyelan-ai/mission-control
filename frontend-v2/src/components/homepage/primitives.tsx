"use client";

/**
 * Homepage UI Primitives — Serious. Flat. No glass, no glow.
 */

import { C } from "./colors";

// ── Section heading ──────────────────────────────────────────────────────────

export function SectionHeading({
  title,
  right,
}: {
  title: string;
  right?: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between mb-2.5">
      <h2 className="text-[11px] font-semibold uppercase tracking-[0.14em]" style={{ color: C.textMuted }}>{title}</h2>
      {right}
    </div>
  );
}

// ── Panel — solid, no glass, no blur ─────────────────────────────────────────

export function Panel({
  children,
  className = "",
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`rounded-lg ${className}`}
      style={{
        background: C.bgSurface,
        border: `1px solid ${C.border}`,
      }}
    >
      {children}
    </div>
  );
}

// ── ServiceDot — flat, no glow ────────────────────────────────────────────────

export function ServiceDot({
  label,
  status,
  detail,
  detailColor,
}: {
  label: string;
  status: string;
  detail: string;
  detailColor?: string;
}) {
  const color = status === "ok" || status === "running"
    ? C.online
    : status === "degraded" || status === "warning"
    ? C.warning
    : status === "error" || status === "down" || status === "offline"
    ? C.error
    : C.textDim;

  return (
    <div className="flex items-center gap-1.5">
      <span className="w-1 h-1 rounded-full shrink-0" style={{ backgroundColor: color }} />
      <span className="text-[10px]" style={{ color: C.textSecondary }}>{label}</span>
      <span className="text-[10px] font-mono tabular-nums" style={{ color: detailColor ?? C.textMuted }}>{detail}</span>
    </div>
  );
}

// ── SparklineChart — flat, no glow, no blur, no animated dots ────────────────

export function SparklineChart({
  data,
  color,
  label,
  value,
  height = 48,
}: {
  data: number[];
  color: string;
  label: string;
  value: string;
  height?: number;
}) {
  if (data.length < 2) {
    return (
      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between">
          <span className="text-[10px] font-medium" style={{ color: C.textMuted }}>{label}</span>
          <span className="text-[10px] font-mono font-semibold tabular-nums" style={{ color }}>{value}</span>
        </div>
        <div className="rounded-sm" style={{ height, backgroundColor: C.bgElevated }} />
      </div>
    );
  }

  const max = Math.max(...data, 1);
  const min = Math.min(...data, 0);
  const range = max - min || 1;
  const w = 200;
  const h = height;
  const pad = 1;

  const points = data.map((v, i) => {
    const x = pad + (i / (data.length - 1)) * (w - pad * 2);
    const y = h - pad - ((v - min) / range) * (h - pad * 2);
    return `${x},${y}`;
  });

  const linePath = `M${points.join(" L")}`;
  const fillPath = `${linePath} L${w - pad},${h} L${pad},${h} Z`;

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-[0.08em]" style={{ color: C.textMuted }}>{label}</span>
        <span className="text-[10px] font-mono font-bold tabular-nums" style={{ color }}>{value}</span>
      </div>
      <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={height} preserveAspectRatio="none">
        <path d={fillPath} fill={`${color}15`} />
        <path d={linePath} fill="none" stroke={color} strokeWidth={1.2} strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </div>
  );
}

// ── Badge — flat ─────────────────────────────────────────────────────────────

export function Badge({
  children,
  color,
}: {
  children: React.ReactNode;
  color: string;
}) {
  return (
    <span className="text-[10px] font-medium px-1.5 py-0.5 rounded-sm" style={{ color, backgroundColor: `${color}15` }} >
      {children}
    </span>
  );
}

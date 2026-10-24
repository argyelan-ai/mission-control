"use client";

// The gate light (concept §6.4): one dot + one word in the head, the reasons
// in one quiet line under it; on the desktop the right column lists the five
// red rules as checks (passed, failed) plus the amber notes.
import { AlertTriangle, Check, X } from "lucide-react";
import { C, STATUS_TEXT } from "@/lib/colors";
import type { Gate, GateReason, GateRule, GateStatus } from "../types";
import type { BenchWords } from "./words";

export const GATE_DOT: Record<GateStatus, string> = {
  green: C.online,
  amber: C.warning,
  red: C.error,
  pending: C.textMuted,
};

/** The private truncation reason never shows twice: the warning block owns it. */
export function visibleGateReasons(gate: Gate, hideTruncation: boolean): GateReason[] {
  return gate.reasons.filter((r) => !(hideTruncation && r.code === "runs_truncated"));
}

export function GateLine({
  gate,
  words,
  hideTruncation,
}: {
  gate: Gate;
  words: BenchWords;
  hideTruncation: boolean;
}) {
  const reasons = visibleGateReasons(gate, hideTruncation);
  return (
    <div data-testid="result-gate" className="flex flex-col gap-1">
      <div className="flex items-center gap-2">
        <span
          aria-hidden
          className="inline-block w-2 h-2 rounded-full shrink-0"
          style={{ backgroundColor: GATE_DOT[gate.status] }}
        />
        <span className="text-sm font-semibold" style={{ color: C.textPrimary }}>
          {words.ts(`gate.${gate.status}`)}
        </span>
      </div>
      {reasons.length > 0 && (
        <p className="text-xs" style={{ color: C.textMuted }}>
          {reasons.map((r) => words.gateReason(r)).join(" · ")}
        </p>
      )}
    </div>
  );
}

const RED_RULES: Exclude<GateRule, "amber">[] = ["R1", "R2", "R3", "R4", "R5"];

/** Desktop "Prüfung" list: each red rule passed or failed, then the amber notes. */
export function GateChecks({
  gate,
  words,
  hideTruncation,
}: {
  gate: Gate;
  words: BenchWords;
  hideTruncation: boolean;
}) {
  if (gate.status === "pending") return null;
  const reasons = visibleGateReasons(gate, hideTruncation);
  const rows: { key: string; tone: "ok" | "err" | "warn"; text: string }[] = [];
  for (const rule of RED_RULES) {
    const failed = reasons.filter((r) => r.rule === rule && r.severity === "red");
    if (failed.length === 0) rows.push({ key: rule, tone: "ok", text: words.tr(`checks.ok${rule}`) });
    else for (const r of failed) rows.push({ key: `${rule}-${r.code}`, tone: "err", text: words.gateReason(r) });
  }
  for (const r of reasons.filter((x) => x.severity === "amber")) {
    rows.push({ key: `amber-${r.code}`, tone: "warn", text: words.gateReason(r) });
  }
  const tone = {
    ok: { Icon: Check, color: C.online },
    err: { Icon: X, color: STATUS_TEXT.error },
    warn: { Icon: AlertTriangle, color: C.warning },
  } as const;
  return (
    <section data-region="result-checks" aria-label={words.tr("checks.label")}>
      <h2 className="label-sys mb-2">{words.tr("checks.label")}</h2>
      <ul className="flex flex-col">
        {rows.map((row) => {
          const { Icon, color } = tone[row.tone];
          return (
            <li key={row.key} className="flex items-start gap-2 py-2 text-sm" style={{ color: C.textSecondary }}>
              <Icon size={14} aria-hidden className="shrink-0 mt-1" style={{ color }} />
              <span>{row.text}</span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

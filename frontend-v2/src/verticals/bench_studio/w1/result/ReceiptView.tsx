"use client";

// The receipt (concept §7.2): every setting of one contestant, frozen
// write-once. Two layouts from the one record — class A (prompt sha,
// temperature, max_tokens, n, quiet box) and class B (brief sha, tools,
// time limit, attempts, harness version, effort observed). Old rows (v0)
// spell their unknowns out ("nicht festgehalten", "offen – vor der Sperre")
// and never claim a v1 property. A field the record does not have and does
// not list as unknown is left out (K3).
import { useState } from "react";
import { ChevronDown, ReceiptText } from "lucide-react";
import { C } from "@/lib/colors";
import type { ContestantResult, Receipt } from "../types";
import { formatBytes, formatDate, formatInt, shortSha } from "./format";
import type { BenchWords } from "./words";

interface ReceiptRow {
  key: string;
  label: string;
  value: React.ReactNode;
  sub?: string | null;
  unknown?: boolean;
}

function unknownRow(r: Receipt, field: string, key: string, label: string, words: BenchWords): ReceiptRow | null {
  const why = r.unknown[field];
  if (why === undefined) return null;
  const sub = words.reason({ code: why, params: {} });
  return { key, label, value: words.tr("receipt.unknown"), sub, unknown: true };
}

function age(min: number, words: BenchWords): string {
  if (min < 60) return words.tr("receipt.ageMinutes", { n: min });
  if (min < 48 * 60) return words.tr("receipt.ageHours", { n: Math.floor(min / 60) });
  return words.tr("receipt.ageDays", { n: Math.floor(min / 1440) });
}

function kv(obj: Record<string, unknown> | null): string | null {
  if (!obj) return null;
  const parts = Object.entries(obj).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`);
  return parts.length ? parts.join(", ") : null;
}

function Sha({ sha }: { sha: string }) {
  return <span className="font-mono">sha {shortSha(sha)}</span>;
}

export function receiptRows(c: ContestantResult, r: Receipt, words: BenchWords): ReceiptRow[] {
  const loc = words.locale;
  const rows: (ReceiptRow | null)[] = [];
  const cls = words.runClass(r.run_class);
  const isB = r.run_class === "B";
  const unknownOr = (field: string, key: string, label: string, present: ReceiptRow | null) =>
    present ?? unknownRow(r, field, key, label, words);

  rows.push({
    key: "class",
    label: words.tr("receipt.class"),
    value: words.tr(isB ? "receipt.classB" : "receipt.classA", { cls }),
  });

  if (!isB) {
    const bits: string[] = [];
    if (r.temperature !== null) bits.push(words.tr("receipt.temp", { v: r.temperature }));
    if (r.max_tokens_rule === "engine") bits.push(words.tr("receipt.maxTokensEngine"));
    else if (r.max_tokens !== null)
      bits.push(
        words.tr(r.max_tokens_rule === "context" ? "receipt.maxTokensContext" : "receipt.maxTokens", {
          v: formatInt(r.max_tokens, loc),
        }),
      );
    if (r.prompt_sha256 || bits.length) {
      rows.push({
        key: "prompt",
        label: words.tr("receipt.prompt"),
        value: (
          <>
            {r.prompt_sha256 && <Sha sha={r.prompt_sha256} />}
            {r.prompt_sha256 && bits.length > 0 && " · "}
            {bits.join(" · ")}
          </>
        ),
      });
    }
    if (r.n_runs !== null) {
      const parts = [String(r.n_runs)];
      if (r.video_run_index !== null) parts.push(words.tr("receipt.videoRun", { v: r.video_run_index }));
      if (r.failures !== null) parts.push(words.tr("receipt.failures", { f: r.failures }));
      rows.push({ key: "runs", label: words.tr("receipt.runs"), value: parts.join(" · ") });
    }
    // Since 25.10.2026 time, not tokens, ends a direct run; older receipts carry none.
    if (r.time_limit_min !== null)
      rows.push({ key: "timeLimit", label: words.tr("receipt.timeLimit"), value: words.tr("receipt.minutes", { v: r.time_limit_min }) });
  } else {
    const bits: string[] = [];
    if (r.tools_allowed !== null) bits.push(words.tr(r.tools_allowed ? "receipt.toolsAllowed" : "receipt.toolsNone"));
    if (r.brief_sha || bits.length) {
      rows.push({
        key: "brief",
        label: words.tr("receipt.brief"),
        value: (
          <>
            {r.brief_sha && <Sha sha={r.brief_sha} />}
            {r.brief_sha && bits.length > 0 && " · "}
            {bits.join(" · ")}
          </>
        ),
      });
    }
    rows.push(
      unknownOr(
        "time_limit_min",
        "timeLimit",
        words.tr("receipt.timeLimit"),
        r.time_limit_min !== null
          ? { key: "timeLimit", label: words.tr("receipt.timeLimit"), value: words.tr("receipt.minutes", { v: r.time_limit_min }) }
          : null,
      ),
    );
    if (r.attempts !== null) rows.push({ key: "attempts", label: words.tr("receipt.attempts"), value: String(r.attempts) });
  }

  if (r.served_model_id) {
    rows.push({
      key: "model",
      label: words.tr("receipt.model"),
      value: r.served_model_id,
      sub: [words.tr("receipt.modelReported"), r.alias ? words.tr("receipt.alias", { alias: r.alias }) : null]
        .filter(Boolean)
        .join(" · "),
    });
  }

  const device = [
    r.device,
    r.locality ? words.ts(`locality.${r.locality}`) : null,
    r.provider,
    r.gpu_mode ? words.tr("receipt.gpuMode", { v: r.gpu_mode }) : null,
  ].filter(Boolean);
  if (device.length) rows.push({ key: "device", label: words.tr("receipt.device"), value: device.join(" · ") });

  const engine = r.engine ? [r.engine, r.engine_version, r.quant, r.recipe_slug].filter(Boolean).join(" · ") : null;
  rows.push(
    unknownOr("engine", "engine", words.tr("receipt.engine"), engine ? { key: "engine", label: words.tr("receipt.engine"), value: engine } : null),
  );
  rows.push(
    unknownOr(
      "spec_decoding",
      "spec",
      words.tr("receipt.spec"),
      r.spec_decoding ? { key: "spec", label: words.tr("receipt.spec"), value: r.spec_decoding } : null,
    ),
  );

  if (isB) {
    const hv = r.harness_version;
    const harness = words.harness(r.harness);
    if (harness) {
      const unknownVersion = !hv && r.unknown.harness_version !== undefined;
      rows.push({
        key: "harness",
        label: words.tr("receipt.harness"),
        value: (
          <>
            {harness}
            {hv && ` ${hv}`}
            {unknownVersion && (
              <>
                {" · "}
                <span style={{ color: C.textSecondary }}>{words.tr("receipt.versionUnknown")}</span>
              </>
            )}
          </>
        ),
      });
    }
    const observed = (r.effort_observed ?? {}) as Record<string, unknown>;
    if (typeof observed.level === "string" && r.harness) {
      rows.push({
        key: "effort",
        label: words.tr("receipt.effort"),
        value: words.tr("effort.session", { harness: r.harness, level: words.level(observed.level) ?? observed.level }),
        sub: words.tr("receipt.observedNotSet"),
      });
    }
  } else {
    const requested = r.effort_requested;
    if (requested) {
      const observed = (r.effort_observed ?? {}) as Record<string, unknown>;
      const sent = kv(r.effort_sent);
      const subs = [sent ? words.tr("receipt.sent", { kv: sent }) : words.tr("receipt.sentNothing")];
      if (observed.thinking === "on" || observed.thinking === "off")
        subs.push(words.tr("receipt.thinkingObserved", { v: words.tr(observed.thinking === "on" ? "receipt.on" : "receipt.off") }));
      rows.push({
        key: "effort",
        label: words.tr("receipt.effort"),
        value: words.effort(c) ?? words.level(requested) ?? requested,
        sub: subs.join(" · "),
      });
    }
    if (r.quiet_box) {
      const value = words.tr(`receipt.quiet_${r.quiet_box}`);
      rows.push({
        key: "quiet",
        label: words.tr("receipt.quietBox"),
        value: r.serving_age_min !== null ? `${value} · ${words.tr("receipt.servingAge", { age: age(r.serving_age_min, words) })}` : value,
      });
    }
  }

  if (r.network_blocked === true) {
    rows.push({
      key: "network",
      label: words.tr("receipt.network"),
      value: words.tr("receipt.networkBlocked", { n: r.network_attempts ?? 0 }),
    });
  } else if (r.network_blocked === false || r.unknown.network !== undefined) {
    const why = r.unknown.network;
    rows.push({
      key: "network",
      label: words.tr("receipt.network"),
      // The shared reason already says "offen – vor der Sperre aufgenommen".
      value: why ? words.reason({ code: why, params: {} }) : words.tr("receipt.networkOpen"),
      unknown: true,
    });
  }

  if (r.libraries_inlined.length || r.html_bytes !== null) {
    const bits = [];
    if (r.libraries_inlined.length) bits.push(words.tr("receipt.libraries", { libs: r.libraries_inlined.join(", ") }));
    if (r.html_bytes !== null) bits.push(formatBytes(r.html_bytes, loc));
    rows.push({ key: "html", label: words.tr("receipt.html"), value: bits.join(" · ") });
  }

  if (r.lib_track === "three-local") {
    // Lane lib:three-local: what MC served, what the page asked for, and that
    // the page needs three.js from the internet anywhere else.
    const subs = [];
    if (r.three_version_mismatch && r.three_requested?.length)
      subs.push(words.tr("receipt.libraryAsked", { versions: r.three_requested.join(", ") }));
    if (r.three_import_map_injected) subs.push(words.tr("receipt.libraryMap"));
    subs.push(words.tr("receipt.libraryOutside"));
    rows.push({
      key: "library",
      label: words.tr("receipt.library"),
      value: words.tr("receipt.libraryLocal", { lib: r.libraries_provided[0] ?? "three.js" }),
      sub: subs.join(" · "),
    });
  }

  const recorded = r.recorded_at ? formatDate(r.recorded_at, loc) : null;
  rows.push({
    key: "method",
    label: words.tr("receipt.method"),
    value: recorded ? `${r.protocol_version} · ${recorded}` : r.protocol_version,
    sub: r.harvested_at
      ? words.tr("receipt.harvestedAt", { date: formatDate(r.harvested_at, loc) })
      : isB
        ? words.tr("receipt.notHarvested")
        : null,
  });

  if (c.receipt_version !== null && c.receipt_checksum) {
    rows.push({
      key: "version",
      label: words.tr("receipt.version"),
      value: (
        <>
          {words.tr("receipt.versionValue", { v: c.receipt_version })}
          {" · "}
          <span className="font-mono">{c.receipt_checksum.slice(0, 8)}</span>
        </>
      ),
    });
  }
  return rows.filter((x): x is ReceiptRow => x !== null);
}

export function ReceiptView({
  contestant,
  title,
  words,
  defaultOpen = false,
}: {
  contestant: ContestantResult;
  title: string;
  words: BenchWords;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const r = contestant.receipt;
  if (!r) return null;
  const bodyId = `receipt-${contestant.key}`;
  return (
    <div style={{ borderTop: `1px solid ${C.border}` }}>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-2 min-h-12 text-sm text-left cursor-pointer"
        style={{ color: C.textPrimary }}
      >
        <ReceiptText size={16} aria-hidden style={{ color: C.textMuted }} />
        <span className="flex-1">{title}</span>
        <ChevronDown
          size={16}
          aria-hidden
          className="transition-transform"
          style={{ color: C.textMuted, transform: open ? "rotate(180deg)" : undefined }}
        />
      </button>
      {open && (
        <dl id={bodyId} data-testid={`receipt-${r.run_class}`} className="flex flex-col pb-2">
          {receiptRows(contestant, r, words).map((row) => (
            <div
              key={row.key}
              className="flex items-start justify-between gap-4 py-3"
              style={{ borderBottom: `1px solid ${C.borderSubtle}` }}
            >
              <dt className="text-sm shrink-0" style={{ color: C.textMuted }}>
                {row.label}
              </dt>
              {/* "unknown" is a fact of the record, not a status: no warning
                  colour (colour means state; engine unknown never turns the
                  gate amber) — the value quiet, the reason small below. */}
              <dd className="flex flex-col items-end text-right min-w-0">
                <span className="text-sm tabular-nums break-words" style={{ color: row.unknown ? C.textSecondary : C.textPrimary }}>
                  {row.value}
                </span>
                {row.sub && (
                  <span className="text-xs" style={{ color: C.textMuted }}>
                    {row.sub}
                  </span>
                )}
              </dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}

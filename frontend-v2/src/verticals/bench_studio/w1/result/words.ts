"use client";

// Words of the result page + X-Kit sheet: every machine code (reason,
// gate rule, harness, level, cost kind) becomes a sentence through i18n —
// shared words from `benchShared` (lead), page words from `benchResult`.
// A code without a word never reaches the screen raw (K10: no jargon).
import { useLocale, useTranslations } from "next-intl";
import { useMemo } from "react";
import type {
  ContestantResult,
  Effort,
  GateReason,
  Harness,
  Money,
  Reason,
  RunClass,
} from "../types";
import { isDirectApi } from "../types";
import { formatClockTime, formatUsd, numLocale } from "./format";

type Params = Record<string, unknown>;

const ISO = /^\d{4}-\d{2}-\d{2}T/;

export function useBenchWords() {
  const ts = useTranslations("benchShared");
  const tr = useTranslations("benchResult");
  const locale = numLocale(useLocale());

  return useMemo(() => {
    /** Params as the i18n strings want them: ISO times → "16:36", fields worded. */
    const values = (params: Params): Record<string, string | number> => {
      const out: Record<string, string | number> = {};
      for (const [k, v] of Object.entries(params ?? {})) {
        if (typeof v === "number") out[k] = v;
        else if (typeof v === "string" && ISO.test(v)) out[k] = formatClockTime(v, locale);
        else if (k === "field" && typeof v === "string")
          out[k] = tr.has(`field.${v}`) ? tr(`field.${v}`) : v;
        else if (Array.isArray(v)) out[k] = v.map((x) => (tr.has(`field.${x}`) ? tr(`field.${x}`) : String(x))).join(", ");
        else if (v !== null && v !== undefined) out[k] = String(v);
      }
      return out;
    };

    /** Any reason code → words; searched in the shared groups first. */
    const reason = (r: Reason | null | undefined): string | null => {
      if (!r) return null;
      const v = values(r.params);
      for (const group of ["reason", "abort", "wait", "blocker", "gate"]) {
        const key = `${group}.${r.code}`;
        if (ts.has(key)) return ts(key, v);
      }
      if (tr.has(`reason.${r.code}`)) return tr(`reason.${r.code}`, v);
      return tr("reason.unknown");
    };

    const gateReason = (g: GateReason): string => {
      const key = `gate.${g.code}`;
      return ts.has(key) ? ts(key, values(g.params)) : tr("reason.unknown");
    };

    const harness = (h: Harness | null | undefined): string | null =>
      h ? (ts.has(`harness.${h}`) ? ts(`harness.${h}`) : h) : null;

    const runClass = (c: RunClass): string => ts(`runClass.${c}`);

    const level = (l: string | null | undefined): string | null =>
      l ? (ts.has(`level.${l}`) ? ts(`level.${l}`) : l) : null;

    /** The effort as the run had it: API = requested (+ thinking observed),
     *  agent = the session level it ran with (D5: shown, never set). */
    const effort = (c: ContestantResult): string | null => {
      const observed = (c.effort.observed ?? {}) as Params;
      const observedLevel = typeof observed.level === "string" ? (observed.level as string) : null;
      if (c.harness && !isDirectApi(c.harness)) {
        if (!observedLevel) return null;
        return tr("effort.session", { harness: c.harness, level: level(observedLevel) ?? observedLevel });
      }
      const requested: Effort = c.effort.requested;
      let word: string;
      if (requested === "default") {
        // One name per level (K10): the sheet's "hoch (Standard)" when the
        // probe knew what the default maps to.
        const merged = c.effort.default_level ?? observedLevel;
        word = merged ? ts("levelIsDefault", { level: level(merged) ?? merged }) : ts("level.default");
      } else {
        word = level(requested) ?? requested;
      }
      const thinking = observed.thinking;
      if (thinking === "on") return tr("effort.withThinking", { level: word, thinking: tr("effort.thinkingYes") });
      if (thinking === "off") return tr("effort.withThinking", { level: word, thinking: tr("effort.thinkingNo") });
      return word;
    };

    /** D3/D4: never a bare $ on local or subscription runs. */
    const money = (m: Money): { value: string; sub: string | null } => {
      const hypo =
        m.hypothetical_usd !== null && m.hypothetical_usd !== undefined
          ? ts("cost.hypothetical", { usd: formatUsd(m.hypothetical_usd) })
          : null;
      switch (m.cost_kind) {
        case "local":
          return { value: ts("cost.local"), sub: hypo };
        case "subscription":
          return { value: ts("cost.subscription"), sub: hypo };
        case "api_price":
          if (m.usd === null) return { value: ts("cost.unknown"), sub: reason(m.reason) };
          return {
            value: ts("cost.api_price", { usd: formatUsd(m.usd) }),
            sub: m.price_date ? tr("numbers.costSource", { date: m.price_date }) : null,
          };
        default:
          return { value: ts("cost.unknown"), sub: reason(m.reason) };
      }
    };

    return { reason, gateReason, harness, runClass, level, effort, money, locale, ts, tr };
  }, [ts, tr, locale]);
}

export type BenchWords = ReturnType<typeof useBenchWords>;

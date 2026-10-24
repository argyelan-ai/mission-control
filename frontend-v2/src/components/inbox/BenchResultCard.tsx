"use client";

/**
 * Bench W1 (package D): "Test fertig" in the Inbox.
 *
 * The backend (package A) files a finished test as
 * Approval(action_type="bench_result", payload {test_id, title, models,
 * state, gate_status, truncation, state_reason}). It is a notice, not a
 * decision: no approve/reject words. The card says WHICH model and whether
 * the result is postable (review S1: after a recipe day several "Pagoda
 * Garden" cards must not look alike), and "Test abgebrochen" when it did not
 * finish. Opening the result (here, or the result page itself) or
 * "Erledigt" resolves it — no side effect for this type. MC has no web
 * push; this is the way a result reaches the operator.
 */

import Link from "next/link";
import { motion } from "framer-motion";
import { useLocale, useTranslations } from "next-intl";
import { ChevronRight, FlaskConical } from "lucide-react";
import { timeAgo } from "@/lib/utils";
import { C, STATUS } from "@/lib/colors";
import { benchResultHref } from "@/lib/benchLinks";
import type { Approval } from "@/lib/types";

export const BENCH_RESULT_ACTION = "bench_result";

export function BenchResultCard({
  approval,
  onResolve,
  loading,
}: {
  approval: Approval;
  onResolve: (status: "approved" | "rejected", note?: string) => void;
  loading?: boolean;
}) {
  const t = useTranslations("benchHome.inbox");
  const tShared = useTranslations("benchShared");
  const locale = useLocale();
  const p = (approval.payload ?? {}) as {
    test_id?: unknown;
    title?: unknown;
    models?: unknown;
    state?: unknown;
    gate_status?: unknown;
    truncation?: unknown;
    state_reason?: unknown;
  };
  const testId = typeof p.test_id === "string" && p.test_id ? p.test_id : null;
  const prompt = typeof p.title === "string" && p.title.trim() ? p.title.trim() : t("untitled");
  const models = Array.isArray(p.models) ? p.models.filter((m): m is string => typeof m === "string" && m !== "") : [];
  const aborted = p.state === "aborted";
  const reasonCode = typeof p.state_reason === "string" ? p.state_reason : null;
  // One state word + its dot (K6): stopped → why; cut at the token limit →
  // look first; else the gate word ("Postbar", "Nicht zum Posten").
  let stateWord: string | null = null;
  let dot: string = STATUS.idle;
  if (aborted) {
    stateWord = reasonCode && tShared.has(`abort.${reasonCode}`) ? tShared(`abort.${reasonCode}`) : null;
  } else if (p.truncation === true) {
    stateWord = t("truncated");
    dot = STATUS.warning;
  } else if (typeof p.gate_status === "string" && tShared.has(`gate.${p.gate_status}`)) {
    stateWord = tShared(`gate.${p.gate_status}`);
    dot = p.gate_status === "green" ? STATUS.online : p.gate_status === "red" ? STATUS.error : STATUS.warning;
  }

  return (
    <motion.div layout initial={{ opacity: 0, x: -8 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: 8, height: 0 }}>
      <div
        className="rounded-lg p-4 flex flex-col gap-3"
        style={{ background: C.bgSurface, border: `1px solid ${C.border}` }}
        data-testid="bench-result-card"
      >
        <div className="flex items-center gap-2 text-xs" style={{ color: C.textMuted }}>
          <FlaskConical size={14} aria-hidden style={{ color: C.textSecondary }} />
          <span className="font-medium" style={{ color: C.textSecondary }}>
            {aborted ? t("typeStopped") : t("type")}
          </span>
          <span className="ml-auto tabular-nums">{timeAgo(approval.created_at, locale)}</span>
        </div>
        <div className="flex flex-col gap-1 min-w-0">
          <div className="text-base font-medium break-words" style={{ color: C.textPrimary }}>
            {models.length ? models.join(" · ") : prompt}
          </div>
          <div className="flex items-center gap-2 text-sm" style={{ color: C.textSecondary }}>
            {stateWord && <span aria-hidden className="w-2 h-2 rounded-full shrink-0" style={{ background: dot }} />}
            <span className="break-words">{[models.length ? prompt : null, stateWord].filter(Boolean).join(" · ")}</span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {testId && (
            <Link
              href={benchResultHref(testId)}
              onClick={() => onResolve("approved")}
              // Bordered, not the accent: several results can wait at once,
              // and the Inbox screen has at most one accent (K11).
              className="inline-flex items-center gap-2 h-11 px-4 rounded-md text-sm font-medium hover:bg-[var(--color-bg-hover)]"
              style={{ color: C.textPrimary, border: `1px solid ${C.borderActive}` }}
            >
              {t("open")}
              <ChevronRight size={16} aria-hidden />
            </Link>
          )}
          <button
            type="button"
            onClick={() => onResolve("approved")}
            disabled={loading}
            className="inline-flex items-center h-11 px-3 rounded-md text-sm cursor-pointer disabled:opacity-50 hover:bg-[var(--color-bg-hover)]"
            style={{ color: C.textSecondary }}
          >
            {t("done")}
          </button>
        </div>
      </div>
    </motion.div>
  );
}

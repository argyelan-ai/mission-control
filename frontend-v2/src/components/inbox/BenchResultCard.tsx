"use client";

/**
 * Bench W1 (package D): "Test fertig" in the Inbox.
 *
 * The backend (package A) files a finished test as
 * Approval(action_type="bench_result", payload {test_id, title}). It is a
 * notice, not a decision: no approve/reject words. Opening the result (the
 * second of the "2 taps from the Inbox") or "Erledigt" resolves it — the
 * resolve has no side effect for this type — so the item leaves the Inbox
 * and the badge count stays honest. MC has no web push; this is the way a
 * result reaches the operator.
 */

import Link from "next/link";
import { motion } from "framer-motion";
import { useLocale, useTranslations } from "next-intl";
import { ChevronRight, FlaskConical } from "lucide-react";
import { timeAgo } from "@/lib/utils";
import { C } from "@/lib/colors";
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
  const locale = useLocale();
  const p = (approval.payload ?? {}) as { test_id?: unknown; title?: unknown };
  const testId = typeof p.test_id === "string" && p.test_id ? p.test_id : null;
  const title = typeof p.title === "string" && p.title.trim() ? p.title.trim() : t("untitled");

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
            {t("type")}
          </span>
          <span className="ml-auto tabular-nums">{timeAgo(approval.created_at, locale)}</span>
        </div>
        <div className="text-base font-medium break-words" style={{ color: C.textPrimary }}>
          {title}
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

"use client";

/**
 * HeadRunRecordCard — the run record as a small facts card at the end of a
 * finished head's chat (bauplan `heads-sichtbar` PR 2 §3.2), fed by
 * `GET /heads/{run_id}/summary` (`services/heads/summary.py`'s own
 * docstring has the field-by-field contract). K3: a field the record never
 * recorded (`null`) is left OUT entirely — never shown as "—" or
 * "not recorded". No state word here (DESIGN.md K3, review finding on the
 * prototype's "A fertig" image — the chat header above already carries
 * it).
 */
import { useState } from "react";
import { useTranslations } from "next-intl";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, Copy, ExternalLink } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C } from "@/lib/colors";
import { prNumberFromUrl } from "@/lib/heads";

function Fact({ label, value }: { label: string; value: string }) {
  return (
    // Review finding on PR #756 (DESIGN.md K3, "nur beim Titel [kürzen]"):
    // the VALUE used to `truncate` (cut mid-word at 393px — a test command
    // or a run-record result line is exactly the kind of fact that does not
    // fit one line) while the LABEL wrapped onto two instead. Flipped: the
    // label is the short, fixed part (`shrink-0`, never wraps), the value
    // is the one allowed to grow and wrap onto more than one line.
    <div className="flex items-start justify-between gap-3 py-1 text-xs">
      <span className="shrink-0 whitespace-nowrap" style={{ color: C.textMuted }}>{label}</span>
      <span className="text-right min-w-0 break-words" style={{ color: C.textSecondary }}>{value}</span>
    </div>
  );
}

export function HeadRunRecordCard({ runId }: { runId: string }) {
  const t = useTranslations("heads");
  const [open, setOpen] = useState(false);

  const summaryQuery = useQuery({
    queryKey: ["heads", runId, "summary"],
    queryFn: () => api.heads.summary(runId),
    retry: false,
    staleTime: Infinity,
  });
  const recordQuery = useQuery({
    queryKey: ["heads", runId, "run-record"],
    queryFn: () => api.heads.runRecord(runId),
    enabled: open,
    retry: false,
  });

  if (summaryQuery.isError || !summaryQuery.data) return null;
  const s = summaryQuery.data;
  const prNumber = prNumberFromUrl(s.pr_url);

  const yn = (v: boolean | null) => (v == null ? null : v ? t("summary.yes") : t("summary.no"));
  const reviewLabel = s.review === "helper" ? t("summary.reviewHelper") : s.review === "self" ? t("summary.reviewSelf") : null;

  return (
    <section
      data-testid="head-run-record-card"
      className="mx-3 md:mx-4 my-3 rounded-lg p-4 space-y-2"
      style={{ background: "var(--color-bg-hover)", border: `1px solid ${C.border}` }}
    >
      <h3 className="text-sm font-semibold" style={{ color: C.textPrimary }}>
        {t("summary.title")}
      </h3>

      {s.result_line && (
        <p className="text-sm leading-snug" style={{ color: C.textSecondary }} data-testid="head-record-result">
          {s.result_line}
        </p>
      )}

      <div className="pt-1">
        {s.tests.failed_before && <Fact label={t("summary.testsRedBefore")} value={s.tests.failed_before} />}
        {s.tests.passed_after && <Fact label={t("summary.testsGreenAfter")} value={s.tests.passed_after} />}
        {yn(s.sabotage) && <Fact label={t("summary.sabotage")} value={yn(s.sabotage) as string} />}
        {s.kz_ok != null && <Fact label={t("summary.kz")} value={yn(s.kz_ok) as string} />}
        {reviewLabel && <Fact label={t("summary.review")} value={reviewLabel} />}
        {s.bypass != null && <Fact label={t("summary.bypass")} value={String(s.bypass)} />}
        {s.operator_minutes != null && <Fact label={t("summary.operatorMinutes")} value={String(s.operator_minutes)} />}
        {s.helpers != null && <Fact label={t("summary.helpers")} value={String(s.helpers)} />}
      </div>

      {s.branch && (
        <div className="flex items-center gap-2 pt-1">
          <span className="text-xs font-mono truncate min-w-0" style={{ color: C.textMuted }}>
            {s.branch}
          </span>
          <button
            type="button"
            aria-label={t("card.copyBranch")}
            data-testid="head-record-copy-branch"
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(s.branch ?? "");
                notify.success(t("card.branchCopied"));
              } catch {
                notify.error(t("errors.unknown"));
              }
            }}
            className="shrink-0 flex items-center justify-center min-w-touch min-h-touch -m-2 rounded-md cursor-pointer"
            style={{ color: C.textSecondary }}
          >
            <Copy size={13} aria-hidden />
          </button>
        </div>
      )}

      {s.pr_url && (
        <a
          href={s.pr_url}
          target="_blank"
          rel="noopener noreferrer"
          data-testid="head-record-open-pr"
          className="inline-flex items-center gap-2 text-xs font-medium"
          style={{ color: C.accent }}
        >
          {prNumber != null ? t("card.openPr", { number: prNumber }) : t("card.openPrNoNumber")}
          <ExternalLink size={13} aria-hidden />
        </a>
      )}

      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        data-testid="head-record-open-full"
        className="flex items-center gap-1 pt-1 text-xs cursor-pointer"
        style={{ color: C.textMuted }}
      >
        {t("summary.openFull")}
        <ChevronDown size={13} aria-hidden style={{ transform: open ? "rotate(180deg)" : undefined, transition: "transform 0.15s" }} />
      </button>
      {open && (
        <pre
          className="rounded-dense p-3 text-xs leading-snug font-mono overflow-auto max-h-[420px] whitespace-pre-wrap break-words"
          style={{ background: C.bgDeep, color: C.textSecondary, border: `1px solid ${C.border}` }}
          data-testid="head-record-full"
        >
          {recordQuery.isError ? t("summary.failed") : recordQuery.data ?? "…"}
        </pre>
      )}
    </section>
  );
}

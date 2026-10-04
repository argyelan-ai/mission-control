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
import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, Copy, ExternalLink } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C } from "@/lib/colors";
import { prNumberFromUrl } from "@/lib/heads";

// text-xs (12px) at leading-snug (1.375) ≈ 16.5px/line, rounded up — two
// lines is the clamp the review asked for (see Fact below).
const FACT_CLAMP_LINES = 2;
const FACT_LINE_HEIGHT_PX = 17;
const FACT_CLAMP_MAX_PX = FACT_CLAMP_LINES * FACT_LINE_HEIGHT_PX;

function Fact({ label, value, t }: { label: string; value: string; t: ReturnType<typeof useTranslations> }) {
  // Review finding on PR #756 round 3: a right-aligned paragraph next to a
  // fixed label (the round-2 fix for DESIGN.md K3's "nothing cut off") still
  // printed a real ~500-character test fact as ten unreadable lines on the
  // phone, backticks and all. Stacked instead: the short label on its own
  // line, the value left-aligned beneath it, clamped to two lines with an
  // expand control when it genuinely overflows — never a silent, permanent
  // cut (K3 bans "…" outside the title; this recovers the rest on tap,
  // which plain `truncate` never did).
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const valueRef = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    const el = valueRef.current;
    if (!el) return;
    const measure = () => {
      // jsdom (and an off-screen `display: none` pane) reports 0 for every
      // layout metric — that must read as "nothing hidden", not "clamp it".
      if (el.scrollHeight === 0) return;
      setOverflows(el.scrollHeight > FACT_CLAMP_MAX_PX + FACT_LINE_HEIGHT_PX / 2);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [value]);

  const clamped = overflows && !expanded;

  return (
    <div className="py-1 text-xs">
      <span className="block" style={{ color: C.textMuted }}>{label}</span>
      <p
        ref={valueRef}
        className={`mt-1 break-words${clamped ? " line-clamp-2" : ""}`}
        style={{ color: C.textSecondary }}
      >
        {value}
      </p>
      {overflows && (
        // On the spacing/type scale this component already uses everywhere
        // else, not an off-grid 2px gap or an arbitrary 11px size — the
        // design ratchet counts either kind of new free value as a regression.
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className="mt-1 min-h-touch text-xs font-medium cursor-pointer"
          style={{ color: C.textMuted }}
        >
          {expanded ? t("summary.collapse") : t("summary.expand")}
        </button>
      )}
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
        {s.tests.failed_before && <Fact label={t("summary.testsRedBefore")} value={s.tests.failed_before} t={t} />}
        {s.tests.passed_after && <Fact label={t("summary.testsGreenAfter")} value={s.tests.passed_after} t={t} />}
        {yn(s.sabotage) && <Fact label={t("summary.sabotage")} value={yn(s.sabotage) as string} t={t} />}
        {s.kz_ok != null && <Fact label={t("summary.kz")} value={yn(s.kz_ok) as string} t={t} />}
        {reviewLabel && <Fact label={t("summary.review")} value={reviewLabel} t={t} />}
        {s.bypass != null && <Fact label={t("summary.bypass")} value={String(s.bypass)} t={t} />}
        {s.operator_minutes != null && <Fact label={t("summary.operatorMinutes")} value={String(s.operator_minutes)} t={t} />}
        {s.helpers != null && <Fact label={t("summary.helpers")} value={String(s.helpers)} t={t} />}
      </div>

      {s.branch && (
        // DESIGN.md K3 (review finding on PR #756 round 3): "…" is only for
        // the title — a `mc-head/<date>-<slug>` branch cut mid-word hid the
        // very thing the copy button exists to let the operator paste
        // whole. `break-all` instead of `truncate`; `items-start` keeps the
        // copy button pinned to the first line once the name wraps.
        <div className="flex items-start gap-2 pt-1">
          <span className="text-xs font-mono break-all min-w-0" style={{ color: C.textMuted }}>
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
        // DESIGN.md K11 (review finding on PR #756 round 3): a `text-xs`
        // link with no padding of its own sits well under the 44px touch
        // target — `min-h-touch` plus centering the row on it, not just the
        // glyph, is what actually grows the tappable area.
        <a
          href={s.pr_url}
          target="_blank"
          rel="noopener noreferrer"
          data-testid="head-record-open-pr"
          className="inline-flex min-h-touch items-center gap-2 text-xs font-medium"
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
        className="flex min-h-touch items-center gap-1 text-xs cursor-pointer"
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

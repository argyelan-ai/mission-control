"use client";

/**
 * SwitchCard — a chat switch (ADR-105) where it happened in the timeline.
 *
 * One card, five states, read from the switch row of MC's record:
 * - `pending`: asked for, the new session is not answering yet — neutral,
 *   the info colour on the dot only;
 * - `done` after a restart: "A → B" plus the size of the handoff package,
 *   which opens on a tap (the operator sees what the new model was told);
 *   a package that had to drop the oldest messages says "n of m";
 * - `done` with a native resume (omp found the old session again): no
 *   package was used, the card says so instead of a size;
 * - `done` model only: the same session, another model;
 * - `failed`: the red card — "Switch failed — still on A", the reason in the
 *   UI language, and on the newest switch the one button "Switch back"
 *   (F6: the old session is still there and resumes natively).
 *
 * Signal doctrine (DESIGN.md K6): one status colour per card. Red is a
 * state here (frame + chip), the button is the calm accent text of the
 * error card's layout B (ChatErrorCard), not a red surface.
 */
import { useState } from "react";
import { AlertTriangle, ArrowRightLeft, ChevronDown, ChevronUp, RotateCcw } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";

import type { SwitchRow } from "@/lib/api";
import { C, STATUS_TEXT } from "@/lib/colors";
import { BusySheet, pairLabel, usePairLookups, useRefusalText, useSwitchAction, type PairLookups } from "./PairPicker";
import { TIMELINE_CARD_GUTTER } from "./timelineCard";

export type SwitchCardKind = "pending" | "done" | "native" | "model" | "failed";

export function switchCardKind(row: Pick<SwitchRow, "status" | "path" | "nativeResume">): SwitchCardKind {
  if (row.status === "pending") return "pending";
  if (row.status === "failed") return "failed";
  if (row.path === "model_only") return "model";
  if (row.nativeResume) return "native";
  return "done";
}

/** "6.2k" / "6,2k" — tokens in the UI language, one decimal from 1000 on. */
export function formatTokens(n: number, locale: string): string {
  if (n < 1000) return String(n);
  return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(n / 1000)}k`;
}

/** Whether the card offers "switch back": a failed restart (or switch
 *  back) that is still the chat's newest switch. A failed model-only switch
 *  left the chat where it was — there is nothing to go back to. */
export function offersSwitchBack(row: Pick<SwitchRow, "status" | "path">, latest: boolean): boolean {
  return latest && row.status === "failed" && (row.path === "restart" || row.path === "switch_back");
}

export function SwitchCardView({
  row,
  latest,
  lookups,
  onSwitchBack,
  backBusy = false,
  backError = null,
}: {
  row: SwitchRow;
  latest: boolean;
  lookups: PairLookups;
  onSwitchBack?: () => void;
  backBusy?: boolean;
  backError?: string | null;
}) {
  const t = useTranslations("cockpit.switch");
  const locale = useLocale();
  const [open, setOpen] = useState(false);
  const kind = switchCardKind(row);
  const from = pairLabel(row.from, lookups) || t("unknownPair");
  const to = pairLabel(row.to, lookups) || t("unknownPair");
  const failed = kind === "failed";
  const code = row.error?.code ?? "generic";
  const reasonKey = `error.${code}`;
  const reason = t.has(reasonKey) ? t(reasonKey) : t("error.generic");
  const tokens = row.realTokens ?? row.estTokens ?? 0;
  const hasPackage = kind === "done" && !!row.package;
  const truncated = !!row.truncated && (row.messagesTotal ?? 0) > 0;

  const title =
    kind === "pending"
      ? t("pendingTitle")
      : kind === "failed"
        ? t("failedTitle", { pair: from })
        : row.path === "switch_back"
          ? t("backTitle")
          : kind === "model"
            ? t("modelTitle")
            : t("doneTitle");

  return (
    <div className={TIMELINE_CARD_GUTTER}>
      <div
        data-testid="switch-card"
        data-kind={kind}
        role={failed ? "alert" : "status"}
        className="rounded-lg px-3 py-2"
        style={{ border: `1px solid ${failed ? C.error : C.border}`, background: C.bgElevated }}
      >
        <div className="flex items-center gap-2">
          {failed ? (
            <AlertTriangle size={14} style={{ color: STATUS_TEXT.error }} aria-hidden="true" />
          ) : (
            <ArrowRightLeft size={14} style={{ color: kind === "pending" ? C.info : C.textMuted }} aria-hidden="true" />
          )}
          <span
            data-testid="switch-card-title"
            className="text-sm font-medium"
            style={{ color: failed ? STATUS_TEXT.error : C.textPrimary }}
          >
            {title}
          </span>
        </div>

        {!failed && (
          <p data-testid="switch-card-pair" className="mt-1 break-words text-sm leading-normal" style={{ color: C.textSecondary }}>
            {t("pair", { from, to })}
          </p>
        )}

        {failed && (
          <p data-testid="switch-card-reason" className="mt-1 break-words text-sm leading-normal" style={{ color: C.textSecondary }}>
            {t("failedBody", { pair: to, reason })}
          </p>
        )}

        {kind === "native" && (
          <p data-testid="switch-card-native" className="mt-1 text-xs" style={{ color: C.textMuted }}>
            {t("nativeResume")}
          </p>
        )}

        {hasPackage && (
          <button
            type="button"
            data-testid="switch-card-package-toggle"
            aria-expanded={open}
            onClick={() => setOpen((o) => !o)}
            className="mt-1 inline-flex min-h-11 items-center gap-1 text-xs cursor-pointer"
            style={{ color: C.textMuted }}
          >
            <span className="tabular-nums">{t("handoff", { tokens: formatTokens(tokens, locale) })}</span>
            <span aria-hidden="true">·</span>
            <span style={{ color: C.accent }}>{open ? t("hide") : t("show")}</span>
            {open ? <ChevronUp size={13} aria-hidden="true" /> : <ChevronDown size={13} aria-hidden="true" />}
          </button>
        )}

        {hasPackage && truncated && (
          <p data-testid="switch-card-truncated" className="text-xs" style={{ color: C.textMuted }}>
            {t("truncated", { included: row.messagesIncluded ?? 0, total: row.messagesTotal ?? 0 })}
          </p>
        )}

        {hasPackage && open && (
          <pre
            data-testid="switch-card-package"
            className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-md px-3 py-2 text-xs leading-normal"
            style={{ background: C.bgDeep, color: C.textSecondary }}
          >
            {row.package}
          </pre>
        )}

        {backError && (
          <p data-testid="switch-card-back-error" role="alert" className="mt-2 text-sm" style={{ color: STATUS_TEXT.error }}>
            {backError}
          </p>
        )}

        {offersSwitchBack(row, latest) && onSwitchBack && (
          <button
            type="button"
            data-testid="switch-card-back"
            onClick={onSwitchBack}
            disabled={backBusy}
            className="mt-2 flex w-full min-h-11 items-center justify-center gap-2 rounded-b-md text-sm font-medium transition-colors hover:bg-[var(--color-bg-hover)] disabled:cursor-default disabled:hover:bg-transparent"
            style={{ color: backBusy ? C.textMuted : C.accent, borderTop: `1px solid ${C.border}` }}
          >
            <RotateCcw size={14} aria-hidden="true" />
            {t("switchBack", { pair: from })}
          </button>
        )}
      </div>
    </div>
  );
}

/** The card in the timeline: labels from the runtime queries, "switch back"
 *  through the same action (and busy sheet) as the picker. */
export function SwitchCard({ info }: { info: { row: SwitchRow; chatId: string; latest: boolean } }) {
  const lookups = usePairLookups();
  const refusalText = useRefusalText();
  const action = useSwitchAction(info.chatId);
  return (
    <>
      <SwitchCardView
        row={info.row}
        latest={info.latest}
        lookups={lookups}
        onSwitchBack={() => action.send({ kind: "back" })}
        backBusy={action.state.phase === "sending"}
        backError={action.state.phase === "refused" ? refusalText(action.state.code) : null}
      />
      <BusySheet
        options={action.state.phase === "busy" ? action.state.options : null}
        onChoose={action.choose}
        onCancel={action.cancel}
      />
    </>
  );
}

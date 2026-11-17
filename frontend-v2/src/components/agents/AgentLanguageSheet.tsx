"use client";

/**
 * The two languages of ONE agent, as a sheet (Settings → Language).
 *
 * operator_language = what the agent writes to the operator (reports,
 * questions, chat replies); work_language = what it writes among agents
 * (comments, reflections, handoffs). Both are per-agent fields and stay the
 * source of truth — this sheet only edits them. Opened from the agent list in
 * Settings → Language and from the agent page, so it carries a link to the
 * other place.
 */
import { useRef, type KeyboardEvent } from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { notify } from "@/lib/notify";
import type { Agent } from "@/lib/types";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { EntityIcon } from "@/components/shared/EntityIcon";
import { sectionHref } from "@/components/settings/sections";

/** Languages the settings offer — mirrors SUPPORTED_LANGUAGES in
 *  backend/app/services/agent_language_defaults.py. */
export const AGENT_LANGUAGES = ["en", "de"] as const;

type LanguageField = "operator_language" | "work_language";
type T = ReturnType<typeof useTranslations>;

/** "German" / "English" in the UI language; an unknown code shows as itself. */
export function languageName(t: T, code: string | undefined): string {
  const c = (code || "en").toLowerCase();
  return t.has(`name.${c}`) ? t(`name.${c}`) : c.toUpperCase();
}

/** One line for an agent: "German to you · English among agents". */
export function languageSummary(t: T, toYou: string | undefined, among: string | undefined): string {
  return t("summary", { toYou: languageName(t, toYou), among: languageName(t, among) });
}

/** Pill radio group, same look as the theme switch (touch size). */
export function LanguageSegmented({
  value,
  options,
  onChange,
  ariaLabel,
  disabled,
}: {
  value: string;
  options: { value: string; label: string }[];
  onChange: (next: string) => void;
  ariaLabel: string;
  disabled?: boolean;
}) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});

  function onKey(e: KeyboardEvent<HTMLButtonElement>, current: string) {
    const step = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
    if (!step) return;
    e.preventDefault();
    const i = options.findIndex((o) => o.value === current);
    const next = options[(i + step + options.length) % options.length].value;
    onChange(next);
    refs.current[next]?.focus();
  }

  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      className="inline-flex items-center gap-1 p-1 shrink-0 rounded-full"
      style={{ background: C.bgBase, border: `1px solid ${C.border}` }}
    >
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            ref={(el) => { refs.current[o.value] = el; }}
            type="button"
            role="radio"
            aria-checked={active}
            tabIndex={active ? 0 : -1}
            disabled={disabled}
            onClick={() => !active && onChange(o.value)}
            onKeyDown={(e) => onKey(e, o.value)}
            className={`inline-flex items-center justify-center min-h-11 min-w-11 px-4 rounded-full text-sm cursor-pointer transition-colors disabled:cursor-default ${active ? "font-semibold" : "font-medium"}`}
            style={{
              background: active ? C.bgHover : "transparent",
              color: active ? C.textPrimary : C.textMuted,
              boxShadow: active ? `inset 0 0 0 1px ${C.borderActive}` : "none",
            }}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export function AgentLanguageSheet({
  agent,
  open,
  onClose,
  from,
}: {
  agent: Pick<Agent, "id" | "name" | "emoji" | "operator_language" | "work_language">;
  open: boolean;
  onClose: () => void;
  /** Where it was opened — the sheet links to the other place. */
  from: "settings" | "agent";
}) {
  const t = useTranslations("settingsLanguage");
  const qc = useQueryClient();
  const options = AGENT_LANGUAGES.map((code) => ({ value: code, label: languageName(t, code) }));

  const mutation = useMutation({
    mutationFn: (data: Partial<Pick<Agent, LanguageField>>) => api.agents.update(agent.id, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agent", agent.id] });
      qc.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: (e: Error) => notify.error(e.message),
  });

  // Show the choice at once; the query refresh confirms it.
  const pending = mutation.isPending ? mutation.variables : undefined;
  const toYou = pending?.operator_language ?? agent.operator_language ?? "en";
  const among = pending?.work_language ?? agent.work_language ?? "en";

  return (
    <ResponsiveModal open={open} onClose={onClose} aria-label={t("sheetLabel", { name: agent.name })} className="sm:max-w-md">
      <div className="px-4 pt-2 pb-6" data-region="agent-language-sheet" data-testid="agent-language-sheet">
        <div className="flex items-center gap-3 min-h-12">
          <EntityIcon value={agent.emoji} size={20} />
          <h2 className="flex-1 min-w-0 truncate text-base font-semibold" style={{ color: C.textPrimary }}>
            {agent.name}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("done")}
            className="grid place-items-center w-11 h-11 -mr-2 cursor-pointer"
            style={{ color: C.textMuted }}
          >
            <X size={20} aria-hidden />
          </button>
        </div>

        <div className="pt-4 space-y-4">
          <div className="flex items-center gap-4">
            <span className="flex-1 text-base" style={{ color: C.textPrimary }}>{t("toYou")}</span>
            <LanguageSegmented
              value={toYou}
              options={options}
              ariaLabel={t("toYou")}
              onChange={(v) => mutation.mutate({ operator_language: v })}
            />
          </div>
          <div className="flex items-center gap-4">
            <span className="flex-1 text-base" style={{ color: C.textPrimary }}>{t("amongAgents")}</span>
            <LanguageSegmented
              value={among}
              options={options}
              ariaLabel={t("amongAgents")}
              onChange={(v) => mutation.mutate({ work_language: v })}
            />
          </div>
        </div>

        <p className="text-xs pt-4" style={{ color: C.textMuted }}>{t("sheetHint")}</p>

        <div className="flex items-center justify-between gap-4 pt-6">
          <Link
            href={from === "settings" ? `/agents/${agent.id}` : sectionHref("language")}
            onClick={onClose}
            className="inline-flex items-center min-h-11 text-sm underline underline-offset-4"
            style={{ color: C.textSecondary }}
          >
            {from === "settings" ? t("openAgent") : t("languageSettings")}
          </Link>
          <button
            type="button"
            onClick={onClose}
            className="inline-flex items-center min-h-11 px-6 rounded-md text-sm font-semibold cursor-pointer"
            style={{ background: C.accent, color: C.onAccent }}
          >
            {t("done")}
          </button>
        </div>
      </div>
    </ResponsiveModal>
  );
}

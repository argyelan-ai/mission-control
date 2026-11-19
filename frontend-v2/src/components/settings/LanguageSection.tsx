"use client";

/**
 * Settings → Language — every language setting in one place (operator
 * decision 16.11.2026, variant A "list + sheet"):
 *
 *   You         app language (cookie) + voice-call language (per-user pref)
 *   New agents  fleet default operator/work language (admin, app_settings)
 *   Agents      each agent's two languages; tap → AgentLanguageSheet
 *
 * The per-agent fields stay the source of truth; the default only seeds a
 * new agent (backend/app/services/agent_language_defaults.py).
 */
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { notify } from "@/lib/notify";
import type { Agent } from "@/lib/types";
import { EntityIcon } from "@/components/shared/EntityIcon";
import {
  AGENT_LANGUAGES,
  AgentLanguageSheet,
  languageName,
  languageSummary,
} from "@/components/agents/AgentLanguageSheet";

// One row shape for the whole section: label left, value right, 48 px, on
// the section's one left edge (K9).
const rowClass = "relative flex items-center gap-4 min-h-12 rounded-lg transition-colors";

/**
 * A settings row that opens the platform's own picker: the native <select>
 * lies invisibly over the whole row, so the phone shows its wheel/list and
 * the keyboard and screen readers get a real combobox.
 */
function SelectRow({
  label,
  value,
  options,
  onChange,
  ariaLabel,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (next: string) => void;
  ariaLabel: string;
}) {
  const current = options.find((o) => o.value === value)?.label ?? value;
  return (
    <li className={`${rowClass} hover:bg-[var(--color-bg-hover)] focus-within:outline focus-within:outline-2 focus-within:outline-[var(--color-border-strong)]`}>
      <span className="flex-1 min-w-0 truncate text-base" style={{ color: C.textPrimary }}>{label}</span>
      <span className="text-base" style={{ color: C.textSecondary }}>{current}</span>
      <ChevronRight size={18} aria-hidden className="shrink-0" style={{ color: C.textMuted }} />
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label={ariaLabel}
        className="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
    </li>
  );
}

function Group({ id, label, hint, children }: { id: string; label: string; hint?: string; children: React.ReactNode }) {
  const headingId = `settings-language-${id}`;
  return (
    <section className="pt-6 first:pt-0" data-region={`settings-language-${id}`}>
      <h3 id={headingId} className="text-sm font-medium pb-1" style={{ color: C.textSecondary }}>{label}</h3>
      <ul aria-labelledby={headingId}>{children}</ul>
      {hint && <p className="text-sm pt-1" style={{ color: C.textMuted }}>{hint}</p>}
    </section>
  );
}

// ── You: app language ───────────────────────────────────────────────────────
// Cookie-based (i18n pattern): writes NEXT_LOCALE and refreshes the server
// tree — no URL change, applies immediately. Options in their own language,
// so someone who cannot read the current one still finds theirs.
function AppLanguageRow() {
  const t = useTranslations("settingsLanguage");
  const locale = useLocale();
  const router = useRouter();
  return (
    <SelectRow
      label={t("app")}
      ariaLabel={t("appAria")}
      value={locale}
      options={AGENT_LANGUAGES.map((code) => ({ value: code, label: t(`endonym.${code}`) }))}
      onChange={(next) => {
        document.cookie = `NEXT_LOCALE=${next}; path=/; max-age=31536000; samesite=lax`;
        router.refresh();
      }}
    />
  );
}

// ── You: voice-call language ────────────────────────────────────────────────
// The key under the generic per-user preference store (routers/settings.py,
// GET/PUT /api/v1/settings/{key}). Value shape: {"language": "auto"|"de"|"en"}.
// "auto" (default) follows the app language; an explicit choice overrides it
// on the call itself (backend/app/services/voice_live.py). Add a language: a
// new code in AGENT_LANGUAGES + its i18n keys + a line in _LANG_LINE.
export const VOICE_LANGUAGE_SETTING_KEY = "voice_language";
type VoiceLanguagePref = "auto" | (typeof AGENT_LANGUAGES)[number];

export function VoiceLanguageRow() {
  const t = useTranslations("settingsLanguage");
  const queryClient = useQueryClient();
  const [value, setValue] = useState<VoiceLanguagePref>("auto");

  const { data } = useQuery({
    queryKey: ["voice-language-preference"],
    queryFn: async (): Promise<VoiceLanguagePref> => {
      try {
        const res = await api.settings.get(VOICE_LANGUAGE_SETTING_KEY);
        const lang = (res.value as { language?: string } | null)?.language;
        return lang === "de" || lang === "en" ? lang : "auto";
      } catch {
        // Nothing saved yet (404) — "auto" is the default, matching the
        // backend's fallback when no preference row exists.
        return "auto";
      }
    },
  });

  useEffect(() => {
    if (data) setValue(data);
  }, [data]);

  const mutation = useMutation({
    mutationFn: (next: VoiceLanguagePref) =>
      api.settings.set(VOICE_LANGUAGE_SETTING_KEY, { language: next }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["voice-language-preference"] });
    },
    onError: (e: Error) => notify.error(e.message),
  });

  return (
    <SelectRow
      label={t("voice")}
      ariaLabel={t("voiceAria")}
      value={value}
      options={[
        { value: "auto", label: t("voiceAuto") },
        ...AGENT_LANGUAGES.map((code) => ({ value: code, label: t(`endonym.${code}`) })),
      ]}
      onChange={(next) => {
        const pref = next as VoiceLanguagePref;
        setValue(pref);
        mutation.mutate(pref);
      }}
    />
  );
}

// ── New agents: fleet default ───────────────────────────────────────────────
function NewAgentDefaults({ toYou, among }: { toYou: string; among: string }) {
  const t = useTranslations("settingsLanguage");
  const qc = useQueryClient();
  const mutation = useMutation({
    mutationFn: (data: Parameters<typeof api.settings.setAgentLanguage>[0]) => api.settings.setAgentLanguage(data),
    onSuccess: (res) => qc.setQueryData(["agent-language-defaults"], res),
    onError: (e: Error) => notify.error(e.message),
  });
  const options = AGENT_LANGUAGES.map((code) => ({ value: code, label: languageName(t, code) }));
  const pending = mutation.isPending ? mutation.variables : undefined;
  return (
    <>
      <SelectRow
        label={t("toYou")}
        ariaLabel={t("toYouAria")}
        value={pending?.operator_language ?? toYou}
        options={options}
        onChange={(v) => mutation.mutate({ operator_language: v })}
      />
      <SelectRow
        label={t("amongAgents")}
        ariaLabel={t("amongAria")}
        value={pending?.work_language ?? among}
        options={options}
        onChange={(v) => mutation.mutate({ work_language: v })}
      />
    </>
  );
}

// ── Agents: one row each ────────────────────────────────────────────────────
function AgentRows({ defaults }: { defaults: { operator_language: string; work_language: string } }) {
  const t = useTranslations("settingsLanguage");
  const [openId, setOpenId] = useState<string | null>(null);
  const { data: agents, isError } = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.agents.list(),
  });

  if (isError) {
    return <li className="text-sm py-2" style={{ color: C.textMuted }}>{t("agentsError")}</li>;
  }
  if (!agents) return null;

  const isDefault = (a: Agent) =>
    (a.operator_language ?? "en") === defaults.operator_language &&
    (a.work_language ?? "en") === defaults.work_language;
  // The ones that differ first — that is what the operator looks for.
  const sorted = [...agents].sort(
    (a, b) => Number(isDefault(a)) - Number(isDefault(b)) || a.name.localeCompare(b.name),
  );
  const open = sorted.find((a) => a.id === openId);

  return (
    <>
      {sorted.map((a) => {
        const plain = isDefault(a);
        return (
          <li key={a.id}>
            <button
              type="button"
              onClick={() => setOpenId(a.id)}
              data-testid="language-agent-row"
              className={`${rowClass} w-full min-h-14 py-1 text-left cursor-pointer hover:bg-[var(--color-bg-hover)]`}
            >
              <EntityIcon value={a.emoji} size={20} />
              <span className="flex-1 min-w-0">
                <span className="block truncate text-base" style={{ color: C.textPrimary }}>{a.name}</span>
                <span className="block truncate text-sm" style={{ color: plain ? C.textMuted : C.textSecondary }}>
                  {plain ? t("summaryDefault") : languageSummary(t, a.operator_language, a.work_language)}
                </span>
              </span>
              <ChevronRight size={18} aria-hidden className="shrink-0" style={{ color: C.textMuted }} />
            </button>
          </li>
        );
      })}
      {open && (
        <AgentLanguageSheet agent={open} open onClose={() => setOpenId(null)} from="settings" />
      )}
    </>
  );
}

export function LanguageSection({ isAdmin }: { isAdmin: boolean }) {
  const t = useTranslations("settingsLanguage");
  const { data } = useQuery({
    queryKey: ["agent-language-defaults"],
    queryFn: api.settings.agentLanguage,
    enabled: isAdmin,
  });
  const defaults = data?.defaults ?? { operator_language: "en", work_language: "en" };

  return (
    <div data-region="settings-language">
      <div className="mb-6">
        <h2 className="display text-2xl font-semibold" style={{ color: C.textPrimary }}>{t("title")}</h2>
        <p className="text-sm mt-2" style={{ color: C.textMuted }}>{t("description")}</p>
      </div>

      <Group id="you" label={t("groupYou")} hint={t("voiceHint")}>
        <AppLanguageRow />
        <VoiceLanguageRow />
      </Group>

      {isAdmin && (
        <>
          <Group id="new" label={t("groupNew")} hint={t("newHint")}>
            <NewAgentDefaults toYou={defaults.operator_language} among={defaults.work_language} />
          </Group>
          <Group id="agents" label={t("groupAgents")} hint={t("agentsHint")}>
            <AgentRows defaults={defaults} />
          </Group>
        </>
      )}
    </div>
  );
}

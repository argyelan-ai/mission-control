"use client";

/**
 * CreateGroupModal — Anlage-Dialog für eine Gruppe (ADR-075).
 *
 * Zweck: aus zwei Angaben — Ziel und Mitglieder — eine arbeitsfähige Gruppe
 * machen. Alles andere (Name, Lebensdauer, Lead, Runden, Budget) hat eine
 * tragfähige Vorgabe und darf leer bleiben.
 *
 * Nicht offensichtliche Entscheidung: Der Mitglieder-Deckel (6) sperrt die
 * Chips NICHT über `disabled`. Ein gesperrter Knopf verschluckt den Klick
 * stumm; hier bleibt jeder Chip anklickbar, die Auswahl wird nur nicht
 * angenommen — der Zähler „Members (6/6)" daneben bleibt stehen und erklärt
 * die Ablehnung selbst. `aria-disabled` sagt Screenreadern dasselbe.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { AlertTriangle, ChevronRight, Loader2 } from "lucide-react";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { EntityIcon } from "@/components/shared/EntityIcon";
import { api } from "@/lib/api";
import { C, STATUS_TEXT } from "@/lib/colors";
import type {
  EligibleMember,
  GroupCreatePayload,
  GroupDetail,
  GroupLifecycle,
} from "@/lib/groupTypes";

/** Harte Obergrenze: mehr Sprecher pro Runde wird teuer und unlesbar. */
const MAX_MEMBERS = 6;
const DEFAULT_MAX_ROUNDS = "3";

interface CreateGroupModalProps {
  open: boolean;
  onClose: () => void;
  onCreated: (group: GroupDetail) => void;
}

export function CreateGroupModal({ open, onClose, onCreated }: CreateGroupModalProps) {
  const t = useTranslations("sessions.groups");

  const [eligible, setEligible] = useState<EligibleMember[]>([]);
  const [goal, setGoal] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [name, setName] = useState("");
  const [lifecycle, setLifecycle] = useState<GroupLifecycle>("one_shot");
  const [leadId, setLeadId] = useState("");
  const [maxRounds, setMaxRounds] = useState(DEFAULT_MAX_ROUNDS);
  const [budget, setBudget] = useState("");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [failed, setFailed] = useState(false);

  const goalRef = useRef<HTMLTextAreaElement>(null);

  // Beim Öffnen: leeres Formular UND frische Kandidatenliste. Wer wählbar ist,
  // entscheidet das Backend (comm_v2-Fähigkeit) — die UI rät hier nicht mit.
  useEffect(() => {
    if (!open) return;
    setGoal("");
    setSelected([]);
    setName("");
    setLifecycle("one_shot");
    setLeadId("");
    setMaxRounds(DEFAULT_MAX_ROUNDS);
    setBudget("");
    setAdvancedOpen(false);
    setSubmitting(false);
    setFailed(false);
    goalRef.current?.focus();

    let cancelled = false;
    api.groups
      .eligibleMembers()
      .then((members) => {
        if (!cancelled) setEligible(members);
      })
      .catch(() => {
        if (!cancelled) setEligible([]);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const toggleMember = useCallback((id: string) => {
    setSelected((prev) => {
      if (prev.includes(id)) return prev.filter((m) => m !== id);
      if (prev.length >= MAX_MEMBERS) return prev; // Deckel: Klick wird ignoriert
      return [...prev, id];
    });
    // Ein abgewähltes Mitglied darf nicht als Lead zurückbleiben.
    setLeadId((lead) => (lead === id ? "" : lead));
  }, []);

  const goalFilled = goal.trim().length > 0;
  const enoughMembers = selected.length >= 2;
  const canSubmit = goalFilled && enoughMembers && !submitting;

  const handleSubmit = useCallback(async () => {
    if (!goal.trim() || selected.length < 2 || submitting) return;
    setSubmitting(true);
    setFailed(false);
    const rounds = Math.max(1, Number(maxRounds) || 1);
    const payload: GroupCreatePayload = {
      goal: goal.trim(),
      member_ids: selected,
      ...(name.trim() ? { name: name.trim() } : {}),
      ...(leadId ? { lead_agent_id: leadId } : {}),
      lifecycle,
      max_rounds: rounds,
      ...(budget.trim() ? { budget_usd: Number(budget) } : {}),
    };
    try {
      const group = await api.groups.create(payload);
      onCreated(group);
      onClose();
    } catch {
      // Absichtlich offen lassen: die Eingaben sind Marks Arbeit, ein
      // geschlossenes Modal würde sie wegwerfen.
      setFailed(true);
    } finally {
      setSubmitting(false);
    }
  }, [goal, selected, submitting, maxRounds, name, leadId, lifecycle, budget, onCreated, onClose]);

  const selectedMembers = eligible.filter((m) => selected.includes(m.id));

  return (
    <ResponsiveModal open={open} onClose={onClose} aria-labelledby="create-group-title">
      <div className="px-5 pt-4 pb-3 shrink-0" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
        <h2 id="create-group-title" className="text-base font-semibold" style={{ color: C.textPrimary }}>
          {t("createTitle")}
        </h2>
      </div>

      <div className="flex flex-col gap-5 px-5 py-4 overflow-y-auto">
        {/* ── 1 · Ziel (Pflicht) ──────────────────────────────────────────── */}
        <div className="flex flex-col gap-1 min-w-0">
          <FieldLabel htmlFor="group-goal">{t("createGoalLabel")}</FieldLabel>
          <textarea
            id="group-goal"
            ref={goalRef}
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            rows={3}
            placeholder={t("createGoalPlaceholder")}
            className="w-full resize-none rounded-md px-3 py-2 text-sm outline-none transition-colors"
            style={inputStyle}
            onFocus={focusOn}
            onBlur={focusOff}
          />
          <FieldHint>{t("createGoalHint")}</FieldHint>
        </div>

        {/* ── 2 · Mitglieder (Pflicht, min. 2, max. 6) ────────────────────── */}
        <div
          role="group"
          aria-label={t("createMembersLabel", { count: selected.length, max: MAX_MEMBERS })}
          className="flex flex-col gap-2 min-w-0"
        >
          <span className="text-[11px] font-medium" style={{ color: C.textSecondary }} aria-hidden>
            {t("createMembersLabel", { count: selected.length, max: MAX_MEMBERS })}
          </span>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-1.5">
            {eligible.map((member) => {
              const isSelected = selected.includes(member.id);
              const capped = !isSelected && selected.length >= MAX_MEMBERS;
              return (
                <button
                  key={member.id}
                  type="button"
                  role="checkbox"
                  aria-checked={isSelected}
                  aria-disabled={capped || undefined}
                  onClick={() => toggleMember(member.id)}
                  className={`flex items-center gap-2 rounded-md px-2.5 py-2 text-left text-[12px] cursor-pointer transition-colors ${
                    capped ? "opacity-40" : ""
                  }`}
                  style={{
                    background: isSelected ? C.accentSubtle : C.bgSurface,
                    border: `1px solid ${isSelected ? C.borderAccent : C.border}`,
                    color: isSelected ? C.textPrimary : C.textSecondary,
                  }}
                >
                  <EntityIcon
                    value={member.emoji}
                    size={14}
                    style={{ color: isSelected ? C.accent : C.textMuted }}
                  />
                  <span className="truncate">{member.name}</span>
                </button>
              );
            })}
          </div>
          <FieldHint>{t("createMembersHint")}</FieldHint>
        </div>

        {/* ── 3 · Optionales mit guten Vorgaben ───────────────────────────── */}
        <div className="flex flex-col gap-1 min-w-0">
          <FieldLabel htmlFor="group-name">{t("createNameLabel")}</FieldLabel>
          <input
            id="group-name"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t("createNamePlaceholder")}
            className="w-full rounded-md px-3 py-2 text-sm outline-none transition-colors"
            style={inputStyle}
            onFocus={focusOn}
            onBlur={focusOff}
          />
        </div>

        <div className="flex flex-col gap-1.5 min-w-0">
          <span className="text-[11px] font-medium" style={{ color: C.textSecondary }}>
            {t("createLifecycleLabel")}
          </span>
          <div
            role="radiogroup"
            aria-label={t("createLifecycleLabel")}
            className="flex items-center rounded-md overflow-hidden"
            style={{ border: `1px solid ${C.border}` }}
          >
            {(
              [
                ["one_shot", t("createLifecycleOneShot")],
                ["standing", t("createLifecycleStanding")],
              ] as [GroupLifecycle, string][]
            ).map(([value, label], i) => {
              const active = lifecycle === value;
              return (
                <button
                  key={value}
                  type="button"
                  role="radio"
                  aria-checked={active}
                  onClick={() => setLifecycle(value)}
                  className="flex-1 px-3 py-2 text-[12px] font-medium cursor-pointer transition-colors"
                  style={{
                    background: active ? C.accentSubtle : "transparent",
                    color: active ? C.textPrimary : C.textSecondary,
                    borderLeft: i > 0 ? `1px solid ${C.border}` : undefined,
                  }}
                >
                  {label}
                </button>
              );
            })}
          </div>
          <FieldHint>
            {lifecycle === "one_shot"
              ? t("createLifecycleOneShotHint")
              : t("createLifecycleStandingHint")}
          </FieldHint>
        </div>

        {/* ── 4 · Erweitert (zugeklappt) ──────────────────────────────────── */}
        <div className="flex flex-col gap-3 min-w-0">
          <button
            type="button"
            onClick={() => setAdvancedOpen((v) => !v)}
            aria-expanded={advancedOpen}
            aria-controls="create-group-advanced"
            className="label-sys flex items-center gap-1 self-start cursor-pointer"
            style={{ color: C.textMuted }}
          >
            <ChevronRight
              size={12}
              aria-hidden
              className="transition-transform"
              style={{ transform: advancedOpen ? "rotate(90deg)" : undefined }}
            />
            {t("createAdvanced")}
          </button>

          {advancedOpen && (
            <div id="create-group-advanced" className="flex flex-col gap-3">
              <div className="flex flex-col gap-1 min-w-0">
                <FieldLabel htmlFor="group-lead">{t("createLeadLabel")}</FieldLabel>
                <select
                  id="group-lead"
                  value={leadId}
                  onChange={(e) => setLeadId(e.target.value)}
                  className="w-full rounded-md px-3 py-2 text-sm outline-none transition-colors cursor-pointer"
                  style={inputStyle}
                  onFocus={focusOn}
                  onBlur={focusOff}
                >
                  {/* Leer = das Backend bestimmt den Lead selbst. */}
                  <option value="">—</option>
                  {selectedMembers.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.name}
                    </option>
                  ))}
                </select>
              </div>

              <div className="grid gap-3 sm:grid-cols-2">
                <div className="flex flex-col gap-1 min-w-0">
                  <FieldLabel htmlFor="group-max-rounds">{t("createMaxRoundsLabel")}</FieldLabel>
                  <input
                    id="group-max-rounds"
                    type="number"
                    min={1}
                    value={maxRounds}
                    onChange={(e) => setMaxRounds(e.target.value)}
                    className="w-full rounded-md px-3 py-2 text-sm outline-none transition-colors"
                    style={inputStyle}
                    onFocus={focusOn}
                    onBlur={focusOff}
                  />
                  <FieldHint>{t("createMaxRoundsHint")}</FieldHint>
                </div>

                <div className="flex flex-col gap-1 min-w-0">
                  <FieldLabel htmlFor="group-budget">{t("createBudgetLabel")}</FieldLabel>
                  <input
                    id="group-budget"
                    type="number"
                    min={0}
                    step="0.5"
                    value={budget}
                    onChange={(e) => setBudget(e.target.value)}
                    className="w-full rounded-md px-3 py-2 text-sm outline-none transition-colors"
                    style={inputStyle}
                    onFocus={focusOn}
                    onBlur={focusOff}
                  />
                  <FieldHint>{t("createBudgetHint")}</FieldHint>
                </div>
              </div>
            </div>
          )}
        </div>

        {failed && (
          <div
            role="alert"
            className="flex items-start gap-2 rounded-md px-3 py-2 text-xs"
            style={{
              border: `1px solid ${C.error}66`,
              background: `${C.error}14`,
              color: STATUS_TEXT.error,
            }}
          >
            <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden />
            <span>{t("createFailed")}</span>
          </div>
        )}
      </div>

      {/* ── Fusszeile ─────────────────────────────────────────────────────── */}
      <div
        className="flex flex-wrap items-center justify-between gap-2 px-5 pt-3 shrink-0"
        style={{
          borderTop: `1px solid ${C.borderSubtle}`,
          paddingBottom: "calc(env(safe-area-inset-bottom) + 0.75rem)",
        }}
      >
        <span className="text-[11px] leading-snug" style={{ color: C.textMuted }}>
          {t("createNothingStarts")}
        </span>
        <button
          type="button"
          onClick={handleSubmit}
          disabled={!canSubmit}
          className="flex items-center gap-1.5 rounded-md px-3.5 py-1.5 text-sm font-semibold cursor-pointer transition disabled:cursor-not-allowed disabled:opacity-40"
          style={{ background: C.accent, color: C.onAccent }}
        >
          {submitting && <Loader2 size={14} className="animate-spin" aria-hidden />}
          {t("createSubmit")}
        </button>
      </div>
    </ResponsiveModal>
  );
}

// ── Feld-Optik — ausschliesslich Tokens, wie in CreateLoopDialog ────────────

/** Label und Hinweis sind bewusst Geschwister statt in ein umschliessendes
 *  <label> gepackt: sonst würde der Hinweistext Teil des zugänglichen Namens
 *  („Goal Required. The group works towards it…") statt eine Beschreibung. */
function FieldLabel({ htmlFor, children }: { htmlFor: string; children: React.ReactNode }) {
  return (
    <label htmlFor={htmlFor} className="text-[11px] font-medium" style={{ color: C.textSecondary }}>
      {children}
    </label>
  );
}

function FieldHint({ children }: { children: React.ReactNode }) {
  return (
    <span className="text-[10.5px] leading-snug" style={{ color: C.textMuted }}>
      {children}
    </span>
  );
}

const inputStyle: React.CSSProperties = {
  background: C.bgDeep,
  border: `1px solid ${C.border}`,
  color: C.textPrimary,
};

function focusOn(e: React.FocusEvent<HTMLElement>) {
  e.currentTarget.style.borderColor = `${C.accent}66`;
}

function focusOff(e: React.FocusEvent<HTMLElement>) {
  e.currentTarget.style.borderColor = C.border;
}

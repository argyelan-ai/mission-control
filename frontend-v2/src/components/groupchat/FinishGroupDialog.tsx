"use client";

/**
 * FinishGroupDialog — was passiert mit einer Gruppe, wenn sie durch ist
 * (ADR-075, Nachtrag 22.08.2026).
 *
 * Operator-Wunsch im Wortlaut: „wenn ich am Schluss die Ergebnisse abnehme
 * sollte ich in der Lage sein selber zu bestimmen ob ich es mit den Daten oder
 * ohne Daten löschen/archivieren möchte, nur den Chat. Diese Ergebnisse
 * sollten auch in unserem Memorysystem aufgenommen werden, embedded, wie die
 * Tasks."
 *
 * Zwei Entscheidungen, bewusst getrennt und in dieser Reihenfolge:
 *
 *   1. Kommt das Ergebnis ins Gedächtnis?  (unabhängig von 2)
 *   2. Was passiert mit dem Arbeitsmaterial? (archivieren / nur Chat / alles)
 *
 * Die Trennung ist der Kern: eine Erkenntnis darf ihr Arbeitsmaterial
 * überleben. Wäre das Merken an „archivieren" gekoppelt, müsste man Müll
 * aufheben, um Wissen zu behalten.
 *
 * Handy zuerst: volle Breite, Tippziele ab 44px, die Auswahl sind grosse
 * Flächen statt Radio-Punkte — auf dem Telefon trifft man Flächen, nicht
 * 16px-Kreise.
 */
import { useState } from "react";
import { useTranslations } from "next-intl";
import { Archive, Check, MessageSquareOff, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { notify } from "@/lib/notify";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import type { GroupDetail } from "@/lib/groupTypes";

export type FinishAction = "archive" | "delete_chat" | "delete_all";

interface FinishGroupDialogProps {
  open: boolean;
  group: GroupDetail;
  messageCount: number;
  onClose: () => void;
  /** Die Gruppe ist weg (alles gelöscht) → Auswahl in der Seite räumen. */
  onGone: () => void;
  /** Die Gruppe lebt weiter, hat sich aber geändert (archiviert/Chat weg). */
  onChanged: (group: GroupDetail) => void;
}

const MEMORY_TYPES = ["research", "insight", "knowledge"] as const;

export function FinishGroupDialog({
  open,
  group,
  messageCount,
  onClose,
  onGone,
  onChanged,
}: FinishGroupDialogProps) {
  const t = useTranslations("sessions.groups");
  const [memorize, setMemorize] = useState(true);
  const [memoryTitle, setMemoryTitle] = useState(group.name ?? "");
  const [memoryType, setMemoryType] = useState<(typeof MEMORY_TYPES)[number]>("research");
  // Archivieren ist die reversible Stufe und darum die Vorauswahl — ein
  // Dialog, der auf „endgültig löschen" steht, wartet nur auf einen Fehlgriff.
  const [action, setAction] = useState<FinishAction>("archive");
  const [busy, setBusy] = useState(false);

  const typeLabel = (kind: string) =>
    kind === "research"
      ? t("finishTypeResearch")
      : kind === "insight"
        ? t("finishTypeInsight")
        : t("finishTypeKnowledge");

  async function submit() {
    setBusy(true);
    try {
      // Erst merken, dann räumen: bricht das Merken ab, ist noch nichts weg.
      if (memorize) {
        try {
          await api.groups.memorize(group.id, {
            title: memoryTitle.trim() || undefined,
            memory_type: memoryType,
          });
        } catch (err) {
          // 422 heisst „es gibt noch kein Ergebnis" — das ist kein Grund, das
          // Aufräumen zu verweigern, aber der Operator muss es erfahren.
          notify.error(
            String(err).includes("422") ? t("finishNoResult") : t("actionFailed"),
          );
          setBusy(false);
          return;
        }
      }

      if (action === "archive") {
        onChanged(await api.groups.archive(group.id));
      } else if (action === "delete_chat") {
        onChanged(await api.groups.remove(group.id, "chat"));
      } else {
        await api.groups.remove(group.id, "all");
        onGone();
      }
      notify.success(t("finishDone"));
      onClose();
    } catch (err) {
      notify.error(String(err).includes("409") ? t("finishRunningBlocked") : t("actionFailed"));
    } finally {
      setBusy(false);
    }
  }

  const choices: { key: FinishAction; icon: typeof Archive; label: string; hint: string }[] = [
    { key: "archive", icon: Archive, label: t("finishArchive"), hint: t("finishArchiveHint") },
    {
      key: "delete_chat",
      icon: MessageSquareOff,
      label: t("finishDeleteChat"),
      hint: t("finishDeleteChatHint"),
    },
    { key: "delete_all", icon: Trash2, label: t("finishDeleteAll"), hint: t("finishDeleteAllHint") },
  ];

  return (
    <ResponsiveModal open={open} onClose={onClose} aria-label={t("finishTitle")}>
      <div className="flex flex-col max-h-[85vh]">
        <div className="px-4 pt-4 pb-3 shrink-0">
          <h2 className="text-[16px] font-semibold" style={{ color: C.textPrimary }}>
            {t("finishTitle")}
          </h2>
          <p className="mt-0.5 text-[12px] truncate" style={{ color: C.textMuted }}>
            {group.name} ·{" "}
            {t("finishSummary", { rounds: group.rounds_completed, messages: messageCount })}
          </p>
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto px-4 pb-2">
          {/* ── 1. Gedächtnis ──────────────────────────────────────────── */}
          <button
            type="button"
            onClick={() => setMemorize((v) => !v)}
            aria-pressed={memorize}
            data-testid="finish-memorize"
            className="w-full flex items-start gap-3 py-3 text-left bg-transparent border-0 cursor-pointer"
          >
            <span
              className="mt-0.5 shrink-0 w-5 h-5 rounded-md flex items-center justify-center"
              style={{
                background: memorize ? C.accentSubtle : "transparent",
                border: `1px solid ${memorize ? C.borderAccent : C.border}`,
                color: C.accent,
              }}
            >
              {memorize && <Check size={13} />}
            </span>
            <span className="min-w-0">
              <span className="block text-[14px]" style={{ color: C.textPrimary }}>
                {t("finishMemorize")}
              </span>
              <span className="block text-[12px] mt-0.5" style={{ color: C.textMuted }}>
                {t("finishMemorizeHint")}
              </span>
            </span>
          </button>

          {memorize && (
            <div className="pl-8 pb-3 space-y-2">
              <label className="block">
                <span className="block text-[11px] mb-1" style={{ color: C.textMuted }}>
                  {t("finishMemoryTitle")}
                </span>
                <input
                  value={memoryTitle}
                  onChange={(e) => setMemoryTitle(e.target.value)}
                  data-testid="finish-memory-title"
                  // min-h-11 = 44px: die Tippziel-Untergrenze, die auf dem
                  // Telefon den Unterschied zwischen Treffen und Danebentippen macht.
                  className="w-full min-h-11 px-3 rounded-lg text-[14px] outline-none"
                  style={{
                    background: C.bgElevated,
                    border: `1px solid ${C.border}`,
                    color: C.textPrimary,
                  }}
                />
              </label>
              <div>
                <span className="block text-[11px] mb-1" style={{ color: C.textMuted }}>
                  {t("finishMemoryType")}
                </span>
                {/* Segmente statt Auswahlliste: drei Werte, die alle sofort
                    sichtbar sein sollen — ein Dropdown versteckt zwei davon. */}
                <div className="flex gap-1">
                  {MEMORY_TYPES.map((kind) => {
                    const active = memoryType === kind;
                    return (
                      <button
                        key={kind}
                        type="button"
                        onClick={() => setMemoryType(kind)}
                        aria-pressed={active}
                        className="flex-1 min-h-11 px-2 rounded-lg text-[12px] cursor-pointer transition-colors"
                        style={{
                          background: active ? C.accentSubtle : "transparent",
                          border: `1px solid ${active ? C.borderAccent : C.border}`,
                          color: active ? C.accent : C.textSecondary,
                        }}
                      >
                        {typeLabel(kind)}
                      </button>
                    );
                  })}
                </div>
              </div>
            </div>
          )}

          {/* ── 2. Arbeitsmaterial ─────────────────────────────────────── */}
          <div className="pt-2 pb-1 text-[11px] font-mono uppercase tracking-wider" style={{ color: C.textMuted }}>
            {t("finishThen")}
          </div>
          <div className="space-y-1.5 pb-2">
            {choices.map(({ key, icon: Icon, label, hint }) => {
              const active = action === key;
              const destructive = key === "delete_all";
              return (
                <button
                  key={key}
                  type="button"
                  onClick={() => setAction(key)}
                  aria-pressed={active}
                  data-testid={`finish-action-${key}`}
                  // Grosse Fläche statt Radio-Punkt: auf dem Telefon trifft
                  // man Flächen, keine 16px-Kreise.
                  className="w-full flex items-start gap-3 p-3 rounded-xl text-left cursor-pointer transition-colors"
                  style={{
                    background: active ? C.accentSubtle : C.bgElevated,
                    border: `1px solid ${active ? C.borderAccent : C.border}`,
                  }}
                >
                  <Icon
                    size={16}
                    className="shrink-0 mt-0.5"
                    style={{ color: active ? C.accent : destructive ? C.error : C.textMuted }}
                  />
                  <span className="min-w-0">
                    <span
                      className="block text-[14px] font-medium"
                      style={{ color: active ? C.accent : C.textPrimary }}
                    >
                      {label}
                    </span>
                    <span className="block text-[12px] mt-0.5" style={{ color: C.textMuted }}>
                      {hint}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </div>

        {/* Fussleiste klebt: auf dem Handy scrollt der Inhalt darüber weg, der
            Bestätigen-Knopf darf dabei nie ausser Reichweite geraten. */}
        <div
          className="shrink-0 flex gap-2 px-4 py-3 border-t"
          style={{ borderColor: C.border }}
        >
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="flex-1 min-h-11 rounded-lg text-[14px] cursor-pointer disabled:opacity-40"
            style={{ border: `1px solid ${C.border}`, color: C.textSecondary }}
          >
            {t("cancel")}
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={busy}
            data-testid="finish-confirm"
            className="flex-1 min-h-11 rounded-lg text-[14px] font-medium cursor-pointer disabled:opacity-40"
            style={{
              background: action === "delete_all" ? "rgba(250,73,66,0.10)" : C.accentSubtle,
              border: `1px solid ${action === "delete_all" ? C.error : C.borderAccent}`,
              color: action === "delete_all" ? C.error : C.accent,
            }}
          >
            {t("finishConfirm")}
          </button>
        </div>
      </div>
    </ResponsiveModal>
  );
}

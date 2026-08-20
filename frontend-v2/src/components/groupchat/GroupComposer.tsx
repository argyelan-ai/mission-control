"use client";

/**
 * GroupComposer — die Eingabezeile eines Gruppenraums: Textfeld mit Auto-Grow,
 * @-Erwähnungs-Palette und Senden. Bewusst NICHT der 1:1-Composer: in einem
 * Gruppenraum gibt es kein einzelnes CLI, dessen Modell, Effort oder Kontext
 * man umschalten könnte — alles, was danach aussieht, wäre eine Attrappe.
 *
 * Nicht offensichtlich: die Rundfunk-Zeile fügt den ÜBERSETZTEN Handle ein
 * („@alle" bzw. „@all"). Das ist nur zulässig, weil das Backend beide Formen
 * kennt (`group_service.BROADCAST_HANDLES`) — ein einsprachiger Vergleich
 * hätte die Nachricht in der englischen Oberfläche still nur an den Lead
 * geschickt. Eingefügtes und angezeigtes Wort müssen deshalb identisch
 * bleiben: eine Palette, die etwas anderes einfügt als sie zeigt, ist eine
 * Falle.
 */

import { useEffect, useRef, useState } from "react";
import { SendHorizonal, Users } from "lucide-react";
import { useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import { EntityIcon } from "@/components/shared/EntityIcon";

/** Höhe des Felds in Zeilen: eine im Ruhezustand, sechs als Deckel. */
const MAX_ROWS = 6;
/** Muss der line-height des Feldes entsprechen (14px × 1.5, gerundet), sonst
 *  endet das Auto-Grow mitten in einer Zeile. */
const LINE_HEIGHT_PX = 22;

/** Fallback, falls der Katalog den Rundfunk-Handle nicht liefert. Das Backend
 *  akzeptiert `alle`/`all`/`everyone` (group_service.BROADCAST_HANDLES) — die
 *  Palette fügt darum den übersetzten Handle ein, nicht einen festen. */
const ALL_HANDLE_FALLBACK = "alle";

export interface GroupComposerMember {
  slug: string;
  name: string;
  emoji: string | null;
}

interface GroupComposerProps {
  members: GroupComposerMember[];
  /** Darf synchron oder asynchron sein — der Raum quittiert die Zustellung,
   *  nicht das Feld. */
  onSend: (text: string) => void | Promise<void>;
  sending?: boolean;
  /** Läuft gerade eine Runde? Dann wird die Nachricht erst zur nächsten
   *  Runde zugestellt — Senden bleibt trotzdem erlaubt. */
  roundRunning?: boolean;
  disabled?: boolean;
}

interface MentionEntry {
  /** Was eingefügt wird. */
  handle: string;
  /** Menschlicher Name bzw. das übersetzte Wort für „alle". */
  label: string;
  emoji: string | null;
  isAll: boolean;
}

interface MentionToken {
  query: string;
  /** Index des „@" im Text. */
  start: number;
}

/** Vergleichsform wie im Backend (chat_inbound._fold): Gross/Klein und
 *  Trennzeichen fallen raus. Entscheidet hier nur, ob der Name neben dem
 *  Handle überhaupt etwas Neues sagt. */
function fold(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, "");
}

/**
 * Das angefangene @-Wort links vom Cursor — oder null.
 *
 * Das „@" muss am Wortanfang stehen: `mail@example` ist eine Adresse, keine
 * Erwähnung, und darf die Palette nicht aufreissen.
 */
function readMentionToken(text: string, caret: number): MentionToken | null {
  const pos = Math.max(0, Math.min(caret, text.length));
  const before = text.slice(0, pos);
  const match = /(?:^|\s)@([^\s@]*)$/.exec(before);
  if (!match) return null;
  return { query: match[1], start: before.length - match[1].length - 1 };
}

export function GroupComposer({
  members,
  onSend,
  sending = false,
  roundRunning = false,
  disabled = false,
}: GroupComposerProps) {
  const t = useTranslations("sessions.groups");
  const [text, setText] = useState("");
  const [caret, setCaret] = useState(0);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [paletteIndex, setPaletteIndex] = useState(0);
  const [focused, setFocused] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  /** Cursorposition, die nach dem nächsten Render gesetzt werden muss — eine
   *  Erwähnung landet mitten im Satz, der Cursor gehört dahinter. */
  const pendingCaret = useRef<number | null>(null);

  const entries: MentionEntry[] = [
    {
      handle: (t("composerMentionAll") || ALL_HANDLE_FALLBACK).trim(),
      label: t("composerMentionAll"),
      emoji: null,
      isAll: true,
    },
    ...members.map((m) => ({ handle: m.slug, label: m.name, emoji: m.emoji, isAll: false })),
  ];

  const token = readMentionToken(text, caret);
  const query = token ? token.query.toLowerCase() : "";
  // Präfix-Suche gegen Handle UND Name: Mark tippt mal den Slug, mal den
  // Namen, den er in der Mitgliederliste sieht.
  const matches = token
    ? entries.filter(
        (e) =>
          e.handle.toLowerCase().startsWith(query) || e.label.toLowerCase().startsWith(query),
      )
    : [];
  const paletteVisible = paletteOpen && matches.length > 0;
  const highlighted = Math.min(paletteIndex, Math.max(matches.length - 1, 0));

  const trimmed = text.trim();
  const canSend = trimmed.length > 0 && !sending && !disabled;

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    const previous = el.style.height;
    el.style.height = "auto";
    // Ein ausgeblendetes Feld misst 0 (die Mobil-Ansicht hält die andere
    // Spalte mit `display: none` montiert). Diesen Wert zurückzuschreiben
    // würde das Feld dauerhaft auf Padding-Höhe festnageln.
    if (el.scrollHeight === 0) {
      el.style.height = previous;
      return;
    }
    el.style.height = `${Math.min(el.scrollHeight, MAX_ROWS * LINE_HEIGHT_PX)}px`;
  }, [text]);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el || pendingCaret.current === null) return;
    el.setSelectionRange(pendingCaret.current, pendingCaret.current);
    pendingCaret.current = null;
  }, [text]);

  function send() {
    if (!canSend) return;
    setText("");
    setCaret(0);
    setPaletteOpen(false);
    // Fehler gehören dem Raum (Toast + Wiedervorlage), nicht dem Feld. Hier
    // nur abfangen, damit eine abgelehnte Zustellung keine unbehandelte
    // Promise-Warnung in der Konsole hinterlässt.
    void Promise.resolve(onSend(trimmed)).catch(() => {});
  }

  function applyMention(entry: MentionEntry) {
    if (!token) return;
    const insert = `@${entry.handle} `;
    const next = text.slice(0, token.start) + insert + text.slice(caret);
    const pos = token.start + insert.length;
    pendingCaret.current = pos;
    setText(next);
    setCaret(pos);
    setPaletteOpen(false);
    textareaRef.current?.focus();
  }

  function handleChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    const value = e.target.value;
    const pos = e.target.selectionStart ?? value.length;
    setText(value);
    setCaret(pos);
    setPaletteOpen(readMentionToken(value, pos) !== null);
    setPaletteIndex(0);
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (paletteVisible) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setPaletteIndex(Math.min(highlighted + 1, matches.length - 1));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setPaletteIndex(Math.max(highlighted - 1, 0));
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        // Solange die Palette führt, gewinnt der markierte Eintrag über beide
        // Standardbedeutungen — sonst schickt Enter eine halbe Erwähnung los.
        e.preventDefault();
        applyMention(matches[highlighted]);
        return;
      }
      if (e.key === "Escape") {
        // Schliesst nur die Palette; das getippte „@…" bleibt stehen.
        e.preventDefault();
        setPaletteOpen(false);
        return;
      }
    }

    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  return (
    <div className="relative px-3 pt-2 pb-3 md:px-4 md:pb-4">
      {paletteVisible && (
        <div
          role="listbox"
          data-testid="mention-palette"
          // `position: absolute` steht INLINE, nicht nur als Utility-Klasse:
          // `.corner-ticks` in globals.css setzt `position: relative` als
          // ungeschichtetes CSS und schlägt damit Tailwinds `@layer
          // utilities` — im 1:1-Composer hat genau das die Palette schon
          // einmal mitten in den Bildschirm gerissen.
          className="absolute bottom-full left-3 right-3 z-20 mb-2 min-w-[220px] overflow-hidden rounded-xl"
          style={{
            position: "absolute",
            backgroundColor: C.bgElevated,
            border: `1px solid ${C.border}`,
            boxShadow: "var(--shadow-elevated)",
          }}
        >
          <div className="max-h-56 overflow-y-auto p-1.5">
            {matches.map((entry, i) => {
              // Der Name steht nur dann daneben, wenn er mehr sagt als der
              // Handle — „@sparky Sparky" ist Lärm, „@alle all" nicht.
              const showLabel = fold(entry.label) !== fold(entry.handle);
              return (
                <button
                  key={entry.handle}
                  type="button"
                  role="option"
                  aria-selected={i === highlighted}
                  data-testid={`mention-item-${entry.handle}`}
                  onMouseEnter={() => setPaletteIndex(i)}
                  // Verhindert, dass das Textfeld den Fokus verliert, bevor
                  // der Klick den Text ersetzt.
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => applyMention(entry)}
                  className="flex w-full cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-left"
                  style={{
                    backgroundColor: i === highlighted ? C.accentSubtle : "transparent",
                  }}
                >
                  {entry.isAll ? (
                    <Users size={16} strokeWidth={1.75} style={{ color: C.textMuted, flexShrink: 0 }} aria-hidden />
                  ) : (
                    <EntityIcon value={entry.emoji} size={16} style={{ color: C.textMuted }} />
                  )}
                  <span className="font-mono text-xs" style={{ color: C.accent }}>
                    @{entry.handle}
                  </span>
                  {showLabel && (
                    <span className="truncate text-[11px]" style={{ color: C.textMuted }}>
                      {entry.label}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* Ein Behälter für Feld und Knopf, damit das Ganze als ein Element
          gelesen wird statt als Werkzeugleiste unter einer Textbox. */}
      <div
        className="flex items-end gap-2 rounded-xl transition-colors"
        style={{
          backgroundColor: C.bgElevated,
          border: `1px solid ${focused ? C.textMuted : C.border}`,
        }}
      >
        <textarea
          ref={textareaRef}
          value={text}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          onSelect={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          rows={1}
          disabled={disabled}
          placeholder={t("composerPlaceholder")}
          aria-label={t("composerPlaceholder")}
          // 16px auf dem Handy ist keine Geschmacksfrage: alles darunter lässt
          // iOS Safari beim Fokus in die Seite zoomen.
          className="min-w-0 flex-1 resize-none bg-transparent px-3.5 py-3 text-[16px] leading-[1.5] outline-none placeholder:text-[var(--color-text-muted)] md:text-[14px]"
          style={{
            color: C.textPrimary,
            caretColor: C.accent,
            // Inline, nicht als Klasse: die `:focus-visible`-Regel in
            // globals.css ist ungeschichtetes CSS und schlägt jede
            // Tailwind-Utility — sonst sitzt ein heller Halo im Feld.
            outline: "none",
            maxHeight: MAX_ROWS * LINE_HEIGHT_PX,
          }}
        />

        <button
          type="button"
          onClick={send}
          disabled={!canSend}
          aria-label={t("composerPlaceholder")}
          className="mb-2 mr-2 inline-flex h-8 w-8 shrink-0 cursor-pointer items-center justify-center rounded-lg transition-opacity disabled:cursor-not-allowed"
          style={{
            backgroundColor: C.accent,
            color: C.onAccent,
            opacity: canSend ? 1 : 0.35,
          }}
        >
          <SendHorizonal size={15} strokeWidth={2} aria-hidden />
        </button>
      </div>

      {roundRunning && (
        // Ruhig und ehrlich: die Nachricht geht raus, sie kommt nur nicht
        // mitten in die laufende Runde. Kein Warnton, das ist kein Fehler.
        <div className="px-1.5 pt-1.5 text-[11px]" style={{ color: C.textMuted }} aria-live="polite">
          {t("composerQueuedNote")}
        </div>
      )}
    </div>
  );
}

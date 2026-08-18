"use client";

import { useEffect, useRef, useState } from "react";
import { Brain, Square, ArrowUp, ChevronDown } from "lucide-react";
import { C, STATUS } from "@/lib/colors";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import {
  extractErrorMessage,
  isAgentBusyError,
  isEffortSwitchFailedError,
  isEffortSwitchRejectedError,
  isInputNotSupportedError,
  isSessionOnlyEffort,
  type ChatCapabilities,
  type ChatModelOption,
  type ChatSlashCommand,
  type StateEvent,
  type UsageEvent,
} from "@/lib/chatTypes";
import { CLAUDE_MODELS, SLASH_COMMANDS, formatCompactTokens, type SlashCommand } from "@/lib/claudeCommands";
import { ContextPanel } from "./ContextPanel";

const MAX_ROWS = 8;
/** Rows of room the input holds at rest, before anything is typed. */
const MIN_ROWS = 2;
// Matches the textarea's own line-height (14px body × 1.5, rounded) so the
// auto-grow ceiling lands on a whole row instead of clipping one mid-glyph.
const LINE_HEIGHT_PX = 22;

// Desktop-app-style context ring — compact circular indicator instead of a
// bar, per the operator's ask to match the Claude Desktop app's look.
const RING_SIZE = 18;
const RING_STROKE = 2;
const RING_RADIUS = (RING_SIZE - RING_STROKE) / 2;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

/**
 * The switchable levels, straight from the server's capability block — no
 * hardcoded list, because it differs per harness and per CLI version and the
 * backend is the same source that validates the switch.
 *
 * Returns `[]` when the agent can't switch at all (every host agent: no pane to
 * drive) or when the backend predates the field. An empty list means the chip
 * shows the level read-only instead of opening an empty picker.
 */
export function resolveEffortLevels(capabilities: ChatCapabilities | null | undefined): string[] {
  if (!capabilities?.canSwitchEffort) return [];
  return (capabilities.effortLevels ?? []).filter(
    (level): level is string => typeof level === "string" && level.trim().length > 0,
  );
}

/**
 * The palette's command list: everything the harness reports (built-ins plus
 * the agent's own skills), falling back to the short static list only while the
 * backend doesn't ship the field.
 *
 * Normalizes the leading slash instead of trusting it: the backend assembles
 * this from more than one source (CLI built-ins, skill directories), and a name
 * arriving as either `compact` or `/compact` must not produce `//compact` or a
 * row the "/"-prefix filter can never match. Entries without a usable name are
 * dropped rather than rendered as an unclickable blank.
 */
export function resolveSlashCommands(
  reported: ChatSlashCommand[] | null | undefined,
): SlashCommand[] {
  if (!reported || reported.length === 0) return SLASH_COMMANDS;
  const normalized = reported
    .filter((cmd): cmd is ChatSlashCommand => typeof cmd?.name === "string" && cmd.name.trim().length > 0)
    .map((cmd) => {
      const bare = cmd.name.trim().replace(/^\/+/, "");
      return { command: `/${bare}`, description: cmd.description?.trim() || "" };
    })
    .filter((cmd) => cmd.command.length > 1);
  return normalized.length > 0 ? normalized : SLASH_COMMANDS;
}

/** One row of the model switcher, after normalization. */
export interface ModelChoice {
  /** Sent as `/model <name>`. */
  name: string;
  label: string;
  /** `null` = the harness didn't say; the row then shows no size suffix. */
  contextWindow: number | null;
}

/**
 * The model switcher's rows, from the harness when it reports them, otherwise
 * the static list without any window sizes.
 *
 * The frontend deliberately keeps no model→window map of its own: such a map is
 * wrong the day a new model ships, and the composer's context ring already
 * learned that lesson (`contextWindow` on the usage event is backend-stamped for
 * the same reason).
 */
export function resolveModelOptions(
  reported: ChatModelOption[] | null | undefined,
): ModelChoice[] {
  const fallback = CLAUDE_MODELS.map((m) => ({ name: m.name, label: m.label, contextWindow: null }));
  if (!reported || reported.length === 0) return fallback;
  const normalized = reported
    .map((option) => {
      const name = (option.command ?? option.name ?? "").trim();
      const window = typeof option.contextWindow === "number" && option.contextWindow > 0
        ? option.contextWindow
        : null;
      return { name, label: option.label?.trim() || name, contextWindow: window };
    })
    .filter((option) => option.name.length > 0);
  return normalized.length > 0 ? normalized : fallback;
}

type RingThreshold = "normal" | "warning" | "error";

function ringThreshold(pct: number): RingThreshold {
  if (pct >= 90) return "error";
  if (pct >= 75) return "warning";
  return "normal";
}

function ringColor(threshold: RingThreshold): string {
  if (threshold === "error") return STATUS.error;
  if (threshold === "warning") return STATUS.warning;
  return C.textDim;
}

interface ComposerProps {
  agentId: string;
  /** Transcript truth — never an optimistic guess of what model is active. */
  usage: UsageEvent | null;
  state: StateEvent | null;
  onSend: (text: string) => void;
  onStop: () => void;
  /** Whether the underlying CLI session is currently live (derived from
   *  `session.aliveness`) — distinct from `state.status`. Gates the primary
   *  button entirely: an ended session can neither be sent to nor interrupted,
   *  so it gets no button at all rather than one that fails. Defaults to
   *  `true` so a caller that hasn't wired the real value yet keeps a usable
   *  composer instead of silently losing its only control. */
  sessionLive?: boolean;
  /** Kann MC den Bildschirm des Agenten lesen? Nur fuer `cli-bridge` (Docker+tmux)
   *  liefert `capture_pane` echten Text. Bei Host-Agenten (Boss, Hermes, Jarvis)
   *  gibt es diesen Kanal nicht: `_compute_pane_state` leitet dort `working`/`idle`
   *  allein aus der Transkript-mtime ab und ist sich dabei IMMER sicher — ein Boss,
   *  der denkt ohne zu schreiben, liest sich als `idle`. Genau dann fehlte bisher
   *  der Stop-Knopf, obwohl er der einzige noetige Griff ist. */
  paneObservable?: boolean;
  /** Server-derived harness capabilities. Drives the effort chip: no
   *  capabilities (or `canSwitchEffort: false`) means the level is shown
   *  read-only rather than as a picker that cannot work. */
  capabilities?: ChatCapabilities | null;
}

/**
 * The input row of the Sessions Chat view: auto-growing textarea, model
 * switcher + context meter, and a "/" command palette matching the
 * Codex/Claude-Desktop pattern: anchored directly above the input, live
 * prefix-filtered as you type, ArrowUp/Down + Enter/Tab/Escape to navigate.
 * Enter sends, Shift+Enter inserts a newline. Text reaches `onSend` raw —
 * CRLF normalization already happens in `api.chat.sendText` (B1), so this
 * never touches it twice.
 *
 * The palette is a plain filtered list, not cmdk — cmdk's own filtering
 * needs a `Command.Input` living inside the `Command` tree to drive its
 * internal search state, but the real input here is the message textarea
 * itself (typing continues in place after "/", exactly like Codex/Claude
 * Desktop), which structurally can't be that descendant. Without it, cmdk's
 * `search` state simply stayed empty forever and every item was shown
 * unfiltered — the bug this component was rewritten to fix.
 */
export function Composer({ agentId, usage, state, onSend, onStop, sessionLive = true, capabilities = null, paneObservable = true }: ComposerProps) {
  const [text, setText] = useState("");
  const [focused, setFocused] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  const [contextOpen, setContextOpen] = useState(false);
  const [effortOpen, setEffortOpen] = useState(false);
  /** Vorschau-Position des Reglers waehrend des Ziehens; `null` = zeige den
   *  echten aktuellen Wert. Erst das Loslassen schickt den Wechsel los. */
  const [draftIndex, setDraftIndex] = useState<number | null>(null);
  /** The level asked for, while the request is in flight. Never used as the
   *  chip's label — the label stays transcript truth. */
  const [pendingEffort, setPendingEffort] = useState<string | null>(null);
  /** Flips to false the first time the backend answers `input_not_supported`
   *  (host agents have no pane to drive). Only the backend knows, so the chip
   *  starts interactive and demotes itself to a labelled read-only value —
   *  better than hiding a control that works for most of the fleet. */
  const [effortSupported, setEffortSupported] = useState(true);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [paletteIndex, setPaletteIndex] = useState(0);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const isWorking = state?.status === "working";
  const hasText = text.trim().length > 0;
  /** The probe ran and could not classify the pane at all (`parse_pane_state`
   *  rule 4). "Working" is not disprovable there, so an interrupt is the
   *  truthful primary action rather than a Send that claims we know better.
   *  Deliberately NOT `!state`: a missing state event means the first probe
   *  tick hasn't landed yet, which is a loading moment, not an unreadable
   *  pane — treating it as unknown would flash a Stop on every mount. */
  const statusUnknown = state?.status === "unknown";
  /** Empty input leaves nothing to send, so the button carries the interrupt. */
  /* Stop wird angeboten, sobald "arbeitet" nicht widerlegbar ist: bestaetigtes
   * `working`, ein unlesbares Pane (`unknown`) — und neu auch jeder Agent, dessen
   * Pane wir grundsaetzlich nicht lesen koennen. Ein deaktivierter Senden-Knopf
   * behauptet dort eine Ruhe, die wir nie festgestellt haben. */
  const showStop = !hasText && (isWorking || statusUnknown || !paneObservable);
  const effortLevels = resolveEffortLevels(capabilities);
  /* Welche Stufe der Chip anzeigt. Das usage-Ereignis der laufenden Session
   * gewinnt (nur es kennt die session-only-Stufen max/ultracode); solange es
   * fehlt — eine frisch gestartete Session hat noch keines — faellt der Chip auf
   * den persistierten Standard aus settings.json zurueck, den das Backend in
   * capabilities.effort mitliefert. Kennt niemand einen Wert, steht dort "auto":
   * genau das, was die CLI ohne Override tut. Vorher hing der GANZE Chip an
   * usage?.effort — ohne usage war der Effort schlicht nicht schaltbar
   * (Operator-Befund 18.08.2026). */
  const currentEffort = usage?.effort ?? capabilities?.effort ?? null;

  /* Position des Reglers: waehrend des Ziehens die Vorschau, sonst der aktuelle
   * Wert. Kennt der Agent noch keine Stufe (frische Session, kein usage), steht
   * der Regler auf der ersten — angezeigt wird trotzdem, was wirklich gilt. */
  const currentEffortIndex = currentEffort ? effortLevels.indexOf(currentEffort) : -1;
  const sliderIndex = draftIndex ?? (currentEffortIndex >= 0 ? currentEffortIndex : 0);

  /* Fuellstand der Saeule im Brain-Knopf: Anteil der aktuellen Stufe an der
   * Stufenleiter des Harness. +1, damit schon "low" sichtbar gefuellt ist —
   * eine leere Saeule ist fuer "unbekannt/auto" reserviert. */
  /* Leiter fuer die reine ANZEIGE — unabhaengig vom Schaltrecht. resolveEffortLevels
   * gated bewusst auf canSwitchEffort (der Regler darf nur Schaltbares anbieten);
   * die Saeule des read-only-Chips braucht die Leiter trotzdem. */
  const displayLevels = (capabilities?.effortLevels ?? [])
    .map((l) => l.trim())
    .filter((l) => l.length > 0);
  const staticFillPct = (() => {
    const idx = currentEffort ? displayLevels.indexOf(currentEffort) : -1;
    return idx >= 0 && displayLevels.length > 0 ? ((idx + 1) / displayLevels.length) * 100 : 0;
  })();

  const effortFillPct =
    currentEffortIndex >= 0 && effortLevels.length > 0
      ? ((currentEffortIndex + 1) / effortLevels.length) * 100
      : 0;

  const commitSlider = () => {
    if (draftIndex == null) return;
    const level = effortLevels[draftIndex];
    setDraftIndex(null);
    if (level) selectEffort(level);
  };
  const slashCommands = resolveSlashCommands(capabilities?.slashCommands);
  const modelOptions = resolveModelOptions(capabilities?.modelOptions);

  // Palette is only ever "/<query>" with no space yet — once a space lands,
  // the user has moved on to arguments and the palette has no business
  // staying up. The query itself (everything after "/") drives the filter.
  const isSlashToken = text.startsWith("/") && !text.includes(" ");
  const paletteQuery = isSlashToken ? text.slice(1).toLowerCase() : "";
  const filteredCommands = isSlashToken
    ? slashCommands.filter((cmd) => cmd.command.slice(1).toLowerCase().startsWith(paletteQuery))
    : [];
  const paletteVisible = paletteOpen && filteredCommands.length > 0;
  const highlightedIndex = Math.min(paletteIndex, Math.max(filteredCommands.length - 1, 0));

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    const previous = el.style.height;
    el.style.height = "auto";
    // A hidden element measures 0 (the mobile stack keeps the non-visible
    // screen mounted with `display: none`). Writing that back would pin the
    // input to its padding height forever, since this effect only re-runs on
    // `text`. Leaving it at the natural rows=1 height is correct until it is
    // measurable again.
    if (el.scrollHeight === 0) {
      el.style.height = previous;
      return;
    }
    const maxHeight = MAX_ROWS * LINE_HEIGHT_PX;
    el.style.height = `${Math.min(el.scrollHeight, maxHeight)}px`;
  }, [text]);

  function send() {
    if (text.trim().length === 0) return;
    onSend(text);
    setText("");
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (paletteVisible) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setPaletteIndex((i) => Math.min(i + 1, filteredCommands.length - 1));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setPaletteIndex((i) => Math.max(i - 1, 0));
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        // Must NOT send/insert a tab while the palette is driving the
        // textarea — the highlighted command wins over both defaults.
        e.preventDefault();
        selectCommand(filteredCommands[highlightedIndex].command);
        return;
      }
      if (e.key === "Escape") {
        // Closes the palette only — the "/…" text the user typed stays put.
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

  function handleChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    const value = e.target.value;
    setText(value);
    if (value.startsWith("/") && !value.includes(" ")) {
      const query = value.slice(1).toLowerCase();
      const hasMatch = slashCommands.some((cmd) => cmd.command.slice(1).toLowerCase().startsWith(query));
      // Highlight resets to the first match on every keystroke — an empty
      // result closes the palette entirely rather than showing "no matches".
      setPaletteOpen(hasMatch);
      setPaletteIndex(0);
    } else if (paletteOpen) {
      setPaletteOpen(false);
    }
  }

  function selectModel(name: string) {
    onSend(`/model ${name}`);
    setModelOpen(false);
  }

  async function selectEffort(level: string) {
    setEffortOpen(false);
    if (level === currentEffort) return;
    setPendingEffort(level);
    try {
      await api.chat.setEffort(agentId, level);
      // Deliberately no optimistic relabel: the next transcript turn reports
      // the level the agent is actually running, and that is the only figure
      // the chip is allowed to show.
    } catch (err) {
      if (isInputNotSupportedError(err)) {
        setEffortSupported(false);
      } else if (isAgentBusyError(err)) {
        // The backend refuses mid-turn rather than interrupting the agent
        // (its own preflight). Nothing failed — the moment was wrong — so this
        // is an info, not the red persistent toast a real failure gets.
        notify.info("Agent arbeitet gerade — nach dem Zug erneut versuchen");
      } else if (isEffortSwitchRejectedError(err)) {
        // The CLI said no AND said why. Passing its own words through beats any
        // wording of ours: it names the actual constraint (a level the current
        // model doesn't support, for instance), which a generic failure hides.
        const reason = extractErrorMessage(err);
        notify.error(reason ? `Effort abgelehnt: ${reason}` : "Effort-Wechsel abgelehnt");
      } else if (isEffortSwitchFailedError(err)) {
        notify.error("Effort-Wechsel nicht bestätigt — im Terminal prüfen");
      } else {
        notify.error("Effort-Wechsel fehlgeschlagen");
      }
    } finally {
      setPendingEffort(null);
    }
  }

  function selectCommand(command: string) {
    setPaletteOpen(false);
    setText(`${command} `);
    textareaRef.current?.focus();
  }

  /* Modell-Label: laufende Session > persistierter Standard aus settings.json
   * (capabilities.model) > "—". Gleiche Kette wie beim Effort — vorher zeigte
   * jede frische Session "—", obwohl das Modell laengst feststand (Operator-
   * Befund 18.08.2026). Steht dort ein Kurz-Alias ("sonnet"), zeigen wir das
   * Label des passenden Dropdown-Eintrags ("Sonnet"); eine volle ID bleibt
   * verbatim. */
  const currentModel = usage?.model ?? capabilities?.model ?? null;
  const modelLabel = currentModel
    ? modelOptions.find((m) => m.name === currentModel)?.label ?? currentModel
    : "—";
  // Backend-stamped, never a frontend model→window guess (see claudeCommands.ts).
  const win = usage?.contextWindow;
  const hasWin = typeof win === "number" && win > 0;

  // CLI ground truth wins when the backend has it; otherwise fall back to
  // our own tokens/window estimate; otherwise there's nothing honest to show.
  const hasCliPct = typeof usage?.usedPct === "number";
  const fallbackPct =
    usage && hasWin ? Math.min((usage.inputTokens / win) * 100, 100) : null;
  const pct = hasCliPct ? (usage!.usedPct as number) : fallbackPct;
  const pctSource: "cli" | "estimate" | null = hasCliPct
    ? usage?.source === "estimate"
      ? "estimate"
      : "cli"
    : fallbackPct != null
      ? "estimate"
      : null;

  const ringThresholdValue = pct != null ? ringThreshold(pct) : "normal";
  const ringStrokeColor = ringColor(ringThresholdValue);
  const ringOffset =
    pct != null ? RING_CIRCUMFERENCE * (1 - Math.min(Math.max(pct, 0), 100) / 100) : RING_CIRCUMFERENCE;

  const tokenDetail =
    usage && hasWin
      ? `≈${formatCompactTokens(usage.inputTokens)}/${formatCompactTokens(win)} belegt. `
      : "";
  const sourceLabel = pctSource === "estimate" ? "Schätzung" : "CLI";
  const ringTitle =
    pct != null
      ? `${tokenDetail}Quelle: ${sourceLabel}. Die CLI-Statuszeile zeigt dagegen den Rest bis zur Auto-Komprimierung an — andere Basis, beide korrekt.`
      : undefined;

  return (
    <div
      data-testid={`composer-${agentId}`}
      // The composer is the app's own floor, not a strip bolted to the
      // timeline: no top border, the pill inside carries the edge. No
      // safe-area padding here — on mobile the app's bottom tab bar sits
      // below this and already owns `env(safe-area-inset-bottom)`; adding it
      // again would double the gap.
      className="relative px-3 pt-2 pb-3 md:pb-4 md:px-4"
    >
      {paletteVisible && (
        <div
          data-testid="slash-palette"
          // Anchored directly above the input: bottom edge sits on the
          // composer container's top edge (= the textarea's top, since the
          // palette itself is taken out of flow), same horizontal span as
          // the textarea (left-3/right-3 mirror the container's own
          // padding), with a 320px floor for narrow layouts.
          //
          // `position: "absolute"` is set INLINE, not just via the
          // Tailwind class: `.corner-ticks` in globals.css sets
          // `position: relative` unlayered (plain CSS, not `@layer
          // utilities`), which beats Tailwind v4's own `.absolute` utility
          // under CSS cascade-layer rules regardless of source order —
          // silently downgrading this to an in-flow relative box, which is
          // what made the palette render detached mid-screen instead of
          // pinned to the input. Inline style always wins, so this can't
          // regress again. (Note for whoever owns CommandPalette.tsx: the
          // ⌘K palette combines `fixed` + `corner-ticks` the same way and
          // likely has the identical latent bug — out of scope here.)
          className="absolute bottom-full left-3 right-3 mb-2 min-w-[320px] rounded-md overflow-hidden corner-ticks z-20"
          style={{
            position: "absolute",
            backgroundColor: C.bgElevated,
            border: `1px solid ${C.border}`,
            boxShadow: "var(--shadow-elevated)",
          }}
        >
          <div className="max-h-56 overflow-y-auto p-1.5">
            {filteredCommands.map((cmd, i) => (
              <button
                key={cmd.command}
                type="button"
                data-testid={`slash-item-${cmd.command}`}
                data-highlighted={i === highlightedIndex}
                onMouseEnter={() => setPaletteIndex(i)}
                onClick={() => selectCommand(cmd.command)}
                className="w-full flex items-center gap-2 px-2 py-1.5 rounded-sm text-xs cursor-pointer font-mono text-left"
                style={{
                  backgroundColor: i === highlightedIndex ? C.accentSubtle : "transparent",
                }}
              >
                <span style={{ color: C.accent }}>{cmd.command}</span>
                <span className="text-[10px] font-medium" style={{ color: C.textMuted }}>
                  {cmd.description}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* The pill: one container holding the input and its controls, so the
          whole thing reads as a single field instead of a toolbar sitting
          under a textarea. Radius is the system cap (10px, --radius-xl).
          The white frame the operator saw was NOT this component's own ring —
          measured live: globals.css draws `outline: 2px solid #EBEBDE` with a
          2px offset on every `:focus-visible` element, and a textarea matches
          that pseudo-class even on a plain mouse click. So a near-white,
          offset, 2px halo appeared just inside the pill on every keystroke
          session. Removing only the local accent border would have left it.
          Now: that outline is suppressed here, and focus is carried by the
          pill's own frame stepping to a neutral grey (text-muted, 4.6:1
          against the pill — perceivable per WCAG 2.4.11 without being a halo,
          and unmistakably grey rather than white). */}
      <div
        className="flex flex-col transition-colors"
        // One step above the island tone: the panel is bg-surface, so the
        // control lifts to bg-elevated rather than sinking into it.
        style={{
          backgroundColor: C.bgElevated,
          borderRadius: "var(--radius-xl)",
          border: `1px solid ${focused ? C.textMuted : C.border}`,
        }}
      >
        <textarea
          ref={textareaRef}
          value={text}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          rows={1}
          placeholder="Nachricht an den Agenten…"
          // 16px on mobile is not a taste call: anything smaller makes iOS
          // Safari zoom the viewport on focus. The placeholder and caret are
          // themed explicitly — the browser's own placeholder (50% alpha of
          // the text colour) lands under 4.5:1 on this background.
          className="w-full resize-none bg-transparent outline-none focus-visible:outline-none px-3.5 pt-3.5 text-[16px] md:text-[14px] leading-[1.5] placeholder:text-[var(--color-text-muted)]"
          style={{
            color: C.textPrimary,
            caretColor: C.accent,
            // INLINE, not a utility class: globals.css's `:where(…):focus-visible`
            // rule is unlayered plain CSS, and unlayered CSS beats Tailwind's
            // `@layer utilities` no matter the specificity — the same cascade-
            // layer trap the slash palette hit with `position: absolute` (see
            // its comment above). `focus-visible:outline-none` was silently
            // ignored; only an inline value wins. Measured, not assumed.
            outline: "none",
            // Two lines of room at rest, not one: a one-line slot made a
            // multi-sentence instruction feel like the wrong place to write it.
            // Auto-grow is unaffected — it only ever sets a height ABOVE this
            // floor, and `min-height` wins whenever the content is shorter.
            minHeight: MIN_ROWS * LINE_HEIGHT_PX,
            maxHeight: MAX_ROWS * LINE_HEIGHT_PX,
          }}
        />

        <div className="flex items-center gap-1.5 px-2.5 pb-2.5 pt-1.5">
          <div className="relative">
            <button
              type="button"
              onClick={() => setModelOpen((v) => !v)}
              aria-haspopup="dialog"
              aria-expanded={modelOpen}
              // Quiet by default: this is a switcher, not an active state, so
              // it doesn't get the accent tint (which in this system means
              // "selected"). It lights up on hover and while open.
              className="inline-flex items-center gap-1 font-mono text-xs font-medium px-2 py-1 rounded-lg cursor-pointer transition-colors"
              style={{
                backgroundColor: modelOpen ? C.bgHover : "transparent",
                color: modelOpen ? C.textPrimary : C.textSecondary,
                border: `1px solid ${C.border}`,
              }}
            >
              {modelLabel}
              <ChevronDown
                size={11}
                className="transition-transform duration-150"
                style={{ transform: modelOpen ? "rotate(180deg)" : undefined }}
              />
            </button>
            {modelOpen && (
              <div
                role="listbox"
                className="absolute bottom-full left-0 mb-1.5 w-36 rounded-lg overflow-hidden z-20 p-1"
                style={{
                  backgroundColor: C.bgElevated,
                  border: `1px solid ${C.border}`,
                  boxShadow: "var(--shadow-elevated)",
                }}
              >
                {modelOptions.map((m) => (
                  <button
                    key={m.name}
                    type="button"
                    role="option"
                    aria-selected={m.name === currentModel}
                    data-model={m.name}
                    onClick={() => selectModel(m.name)}
                    className="w-full flex items-center gap-3 text-left px-2 py-1.5 text-[12px] font-mono rounded-md cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
                    style={{
                      color: m.name === currentModel ? C.accent : C.textPrimary,
                      backgroundColor: m.name === currentModel ? C.accentSubtle : "transparent",
                    }}
                  >
                    <span className="min-w-0 truncate">{m.label}</span>
                    {/* The window belongs next to the model it describes —
                        "which one has room for this task" is the actual question
                        behind switching. Right-aligned and muted so the list
                        still reads as a list of models, with a fact attached.
                        Unknown window = no suffix; never an invented number. */}
                    {m.contextWindow != null && (
                      <span
                        className="ml-auto shrink-0 tabular-nums"
                        style={{ color: C.textMuted }}
                      >
                        {formatCompactTokens(m.contextWindow)}
                      </span>
                    )}
                  </button>
                ))}
              </div>
            )}
          </div>

          {usage && pct != null && (
            // The ring stays the compact indicator and the tooltip stays the
            // quick glance; the click opens the breakdown — "how full" and
            // "with what" are two questions, and only the first fits in 18px.
            // `relative` is what the desktop popover anchors to.
            <div className="relative shrink-0">
              <button
                type="button"
                data-context-trigger
                aria-haspopup="dialog"
                aria-expanded={contextOpen}
                // The ring's own `role="progressbar"` is unreachable inside a
                // button (the button's name wins), so the figure has to live in
                // that name — otherwise a screen-reader user learns only that
                // there is a breakdown, never how full the window is.
                aria-label={`Kontext: ${Math.round(pct)}% belegt`}
                onClick={() => setContextOpen((v) => !v)}
                className="flex items-center gap-1 pl-0.5 cursor-pointer rounded-md"
              >
                <div
                  data-testid="context-ring"
                  role="progressbar"
                  aria-valuenow={Math.round(pct)}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  data-threshold={ringThresholdValue}
                  data-source={pctSource ?? undefined}
                  title={ringTitle}
                  className="flex items-center gap-1 shrink-0"
                >
                  <svg width={RING_SIZE} height={RING_SIZE} viewBox={`0 0 ${RING_SIZE} ${RING_SIZE}`}>
                    <circle
                      cx={RING_SIZE / 2}
                      cy={RING_SIZE / 2}
                      r={RING_RADIUS}
                      fill="none"
                      stroke={C.bgHover}
                      strokeWidth={RING_STROKE}
                    />
                    <circle
                      data-testid="context-ring-arc"
                      cx={RING_SIZE / 2}
                      cy={RING_SIZE / 2}
                      r={RING_RADIUS}
                      fill="none"
                      stroke={ringStrokeColor}
                      strokeWidth={RING_STROKE}
                      strokeLinecap="round"
                      strokeDasharray={RING_CIRCUMFERENCE}
                      strokeDashoffset={ringOffset}
                      transform={`rotate(-90 ${RING_SIZE / 2} ${RING_SIZE / 2})`}
                    />
                  </svg>
                  <span
                    data-testid="context-ring-pct"
                    className="font-mono text-[10px] font-medium tabular-nums"
                    style={{ color: C.textMuted }}
                  >
                    {Math.round(pct)}%
                  </span>
                </div>
              </button>
              {contextOpen && (
                <ContextPanel
                  usage={usage}
                  pct={pct}
                  pctSource={pctSource}
                  onClose={() => setContextOpen(false)}
                />
              )}
            </div>
          )}

          {/* ONE morphing primary button, the Claude Code / Desktop pattern.
              Two buttons side by side asked the operator to aim; a single
              control at a fixed spot means the thing under the cursor is always
              "the thing to do next".

              Empty input while the agent works => Stop, because there is
              nothing to send and interrupting is the only useful action. Same
              for an UNCLASSIFIABLE pane (`status: "unknown"`): working can't be
              disproved there, and a disabled Send would assert idleness we
              haven't established.
              The moment there IS text, it becomes Send even mid-turn: steering
              a working agent with a normal message is legitimate and is exactly
              what the terminal allows. Idle with empty input => a ghost Send,
              since the accent means "this is the action" and there is none yet.

              Nothing at all once the session has ended — neither sending nor
              interrupting can reach a session that is over.

              Host agents (Boss, Hermes, Jarvis) are covered via
              `paneObservable={false}`: `capture_pane` returns None for them, so
              `_compute_pane_state` reports a confident `working`/`idle` from
              transcript mtime alone and never `unknown`
              (transcript_chat.py:1285-1291) — a Boss that thinks without writing
              read as `idle` and left no way to interrupt. The cost is a Stop that
              is also visible while such an agent rests; operator-decided
              (18.08.2026): a control that is missing when you need it is worse
              than one that waits. */}
          <div className="ml-auto flex items-center gap-1.5">
          {(currentEffort || (effortSupported && effortLevels.length > 0)) &&
            // A picker needs levels to offer. No capabilities, `canSwitchEffort:
            // false`, or an empty list all mean the same thing here: show the
            // level, don't pretend it can be changed. Umgekehrt gilt: kann der
            // Agent umschalten, gehoert der Chip hin — auch bevor die Session
            // ihr erstes usage-Ereignis geschrieben hat.
            (effortSupported && effortLevels.length > 0 ? (
              <div className="relative">
                <button
                  type="button"
                  onClick={() => setEffortOpen((v) => !v)}
                  aria-haspopup="listbox"
                  aria-expanded={effortOpen}
                  aria-label={`Effort-Stufe: ${currentEffort ?? "auto"}`}
                  data-testid="effort-chip"
                  data-level={currentEffort ?? "auto"}
                  data-pending={pendingEffort != null}
                  disabled={pendingEffort != null}
                  className="inline-flex items-center justify-center gap-1 w-12 h-9 md:h-8 rounded-full cursor-pointer transition-colors disabled:cursor-wait"
                  style={{
                    backgroundColor: effortOpen ? C.bgHover : "transparent",
                    color: pendingEffort != null ? C.textMuted : effortOpen ? C.textPrimary : C.textSecondary,
                    border: `1px solid ${C.border}`,
                    opacity: pendingEffort != null ? 0.6 : 1,
                  }}
                >
                  <Brain size={15} strokeWidth={1.75} aria-hidden="true" />
                  {/* Fuellstands-Saeule (Vorbild Claude-Desktop-App): die Hoehe
                      des gruenen Segments IST die Stufe — von unten gefuellt,
                      low = kleiner Rest, ultracode = voll. Ohne bekannte Stufe
                      ("auto") bleibt die Saeule leer: nichts behaupten. Der
                      exakte Wert steht beim Oeffnen am Regler und fuer
                      Screenreader im aria-label. */}
                  <span
                    aria-hidden="true"
                    className="relative inline-block w-[4px] h-[15px] rounded-full overflow-hidden shrink-0"
                    style={{ backgroundColor: C.bgHover, border: `1px solid ${C.borderSubtle}` }}
                  >
                    <span
                      data-testid="effort-gauge-fill"
                      className="absolute bottom-0 left-0 right-0 rounded-full transition-[height] duration-200"
                      style={{
                        height: `${effortFillPct}%`,
                        backgroundColor: STATUS.online,
                      }}
                    />
                  </span>
                </button>
                {effortOpen && (
                  /* Schieberegler statt Liste (Operator-Wunsch 18.08.2026, nach
                     dem Vorbild der Claude-Desktop-App). Die Stufen sind eine
                     ECHTE Reihenfolge — von sparsam nach gruendlich — und ein
                     Regler zeigt diese Ordnung, wo eine Liste sie nur behauptet.
                     Die Stufen kommen weiterhin ausschliesslich aus den
                     Capabilities des Harness, nichts davon steht hier fest. */
                  <div
                    data-testid="effort-menu"
                    className="absolute bottom-full right-0 mb-1.5 w-64 rounded-lg z-20 p-3"
                    style={{
                      backgroundColor: C.bgElevated,
                      border: `1px solid ${C.border}`,
                      boxShadow: "var(--shadow-elevated)",
                    }}
                  >
                    <input
                      type="range"
                      min={0}
                      max={effortLevels.length - 1}
                      step={1}
                      value={sliderIndex}
                      list="effort-ticks"
                      data-testid="effort-slider"
                      aria-label="Effort-Stufe"
                      aria-valuetext={effortLevels[sliderIndex]}
                      disabled={pendingEffort != null}
                      autoFocus
                      /* Ziehen aendert nur die Vorschau. Gesendet wird erst beim
                         Loslassen — sonst schickt ein einziger Zug ueber den
                         Regler fuenf Umschalt-Befehle in die TUI. */
                      onChange={(e) => setDraftIndex(Number(e.target.value))}
                      onPointerUp={commitSlider}
                      onKeyUp={commitSlider}
                      onBlur={commitSlider}
                      className="w-full cursor-pointer accent-[var(--color-accent)] disabled:cursor-wait"
                    />
                    <div className="flex justify-between mt-1 font-mono text-[10px]" style={{ color: C.textMuted }}>
                      <span>{effortLevels[0]}</span>
                      <span>{effortLevels[effortLevels.length - 1]}</span>
                    </div>
                    <div className="mt-2 flex items-baseline gap-2">
                      <span
                        data-testid="effort-slider-value"
                        className="font-mono text-xs"
                        style={{ color: C.accent }}
                      >
                        {effortLevels[sliderIndex]}
                      </span>
                      {/* Pro Stufe, nicht pauschal: low/medium/high/xhigh
                          schreiben den persistierten Standard des Agenten um,
                          max/ultracode gelten nur fuer die laufende Session
                          (CLI-Design, empirisch belegt in agent_chat_input.py).
                          Genau diese Frage — "ueberlebt das meine Session?" —
                          muss am gewaehlten Wert stehen, nicht im Kleingedruckten. */}
                      <span className="text-[10px]" style={{ color: C.textMuted }}>
                        {isSessionOnlyEffort(effortLevels[sliderIndex])
                          ? "nur diese Session"
                          : "wird Standard"}
                      </span>
                      {pendingEffort != null && (
                        /* "…" solange der Wechsel unterwegs ist. Ein Haken
                           erscheint nie vorab — bestaetigt wird erst, was das
                           Transkript meldet. */
                        <span className="ml-auto text-[10px]" style={{ color: C.textMuted }}>
                          wird gesetzt …
                        </span>
                      )}
                    </div>
                  </div>
                )}
              </div>
            ) : displayLevels.length > 0 ? (
              /* Read-only-Variante des Brain-Chips (Operator-Wunsch 18.08.2026:
                 Boss zeigte das nackte Alt-Label). Gleiche Optik wie der
                 schaltbare Knopf — Gehirn + Saeule — aber als span ohne Aktion:
                 die Leiter kommt vom Backend (canSwitchEffort=false heisst
                 "kennt der Harness", nicht "darfst du druecken"), die Stufe aus
                 dem usage-Ereignis. Der Tooltip sagt ehrlich, warum hier nichts
                 zu klicken ist. */
              <span
                data-testid="effort-chip-static"
                data-level={currentEffort ?? "auto"}
                aria-label={`Effort-Stufe: ${currentEffort ?? "auto"} (nicht umschaltbar)`}
                title="Effort lässt sich für diesen Agenten nicht über den Chat umstellen — seine Runtime hat kein steuerbares Terminal."
                className="inline-flex items-center justify-center gap-1 w-12 h-9 md:h-8 rounded-full cursor-default"
                style={{ color: C.textMuted, border: `1px solid ${C.border}` }}
              >
                <Brain size={15} strokeWidth={1.75} aria-hidden="true" />
                <span
                  aria-hidden="true"
                  className="relative inline-block w-[4px] h-[15px] rounded-full overflow-hidden shrink-0"
                  style={{ backgroundColor: C.bgHover, border: `1px solid ${C.borderSubtle}` }}
                >
                  <span
                    data-testid="effort-gauge-fill-static"
                    className="absolute bottom-0 left-0 right-0 rounded-full"
                    style={{ height: `${staticFillPct}%`, backgroundColor: STATUS.online }}
                  />
                </span>
              </span>
            ) : (
              /* Aeltere Backends ohne Stufenleiter in den Capabilities: ohne
                 Leiter waere jede Saeulen-Fuellung geraten — dann lieber der
                 ehrliche Text. */
              <span
                data-testid="effort-chip-static"
                title="Effort lässt sich für diesen Agenten nicht über den Chat umstellen — seine Runtime hat kein steuerbares Terminal."
                className="font-mono text-xs font-medium px-2 py-1 rounded-lg"
                style={{ color: C.textMuted, border: `1px solid ${C.border}` }}
              >
                {currentEffort}
              </span>
            ))}
            {sessionLive && (
              !showStop ? (
                <button
                  type="button"
                  onClick={send}
                  aria-label="Senden"
                  disabled={!hasText}
                  data-empty={!hasText}
                  data-testid="send-button"
                  className="inline-flex items-center justify-center w-9 h-9 md:w-8 md:h-8 rounded-full cursor-pointer disabled:cursor-not-allowed transition-colors"
                  style={
                    hasText
                      ? { backgroundColor: C.accent, color: C.onAccent }
                      : {
                          backgroundColor: "transparent",
                          color: C.textDim,
                          border: `1px solid ${C.border}`,
                        }
                  }
                >
                  <ArrowUp size={16} strokeWidth={2.25} />
                </button>
              ) : (
                <button
                  type="button"
                  onClick={onStop}
                  aria-label="Stop"
                  title="Unterbrechen (ESC)"
                  data-testid="stop-button-prominent"
                  data-reason={isWorking ? "working" : !paneObservable ? "unobservable" : "unknown"}
                  /* The pulse is a claim of live activity, so it only runs when
                     the probe actually confirmed "working". On an unreadable
                     pane the control is offered without animating something we
                     don't know. */
                  className={`${isWorking ? "animate-pulse " : ""}inline-flex items-center justify-center w-9 h-9 md:w-8 md:h-8 rounded-full cursor-pointer`}
                  style={{
                    backgroundColor: C.accentSubtle,
                    color: C.textPrimary,
                    border: `1px solid ${C.borderAccent}`,
                  }}
                >
                  <Square size={12} fill={C.textPrimary} />
                </button>
              )
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

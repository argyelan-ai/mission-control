"use client";

import { useEffect, useRef, useState } from "react";
import { Square, ArrowUp, Check, ChevronDown } from "lucide-react";
import { C, STATUS } from "@/lib/colors";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import {
  isAgentBusyError,
  isEffortSwitchFailedError,
  isInputNotSupportedError,
  isSessionOnlyEffort,
  type ChatCapabilities,
  type StateEvent,
  type UsageEvent,
} from "@/lib/chatTypes";
import { CLAUDE_MODELS, SLASH_COMMANDS, formatCompactTokens } from "@/lib/claudeCommands";
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
  /** Whether the underlying CLI session is currently live (from
   *  `session.live`) — distinct from `state.status`. Boss has no pane probe
   *  in v1 (mtime heuristic only), so `state.status === "working"` is often
   *  missed while he's actually busy; gating Stop on that alone hides the
   *  one control that would actually help. Whenever the session is live the
   *  Stop button stays reachable — prominent while `state.status ===
   *  "working"`, a quiet secondary icon otherwise. Never rendered when the
   *  session isn't live. Defaults to `true` so callers that haven't wired up
   *  the real `session.live` value yet keep the previous working-only
   *  behavior instead of silently losing the button. */
  sessionLive?: boolean;
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
export function Composer({ agentId, usage, state, onSend, onStop, sessionLive = true, capabilities = null }: ComposerProps) {
  const [text, setText] = useState("");
  const [focused, setFocused] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  const [contextOpen, setContextOpen] = useState(false);
  const [effortOpen, setEffortOpen] = useState(false);
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
  const effortLevels = resolveEffortLevels(capabilities);

  // Palette is only ever "/<query>" with no space yet — once a space lands,
  // the user has moved on to arguments and the palette has no business
  // staying up. The query itself (everything after "/") drives the filter.
  const isSlashToken = text.startsWith("/") && !text.includes(" ");
  const paletteQuery = isSlashToken ? text.slice(1).toLowerCase() : "";
  const filteredCommands = isSlashToken
    ? SLASH_COMMANDS.filter((cmd) => cmd.command.slice(1).toLowerCase().startsWith(paletteQuery))
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
      const hasMatch = SLASH_COMMANDS.some((cmd) => cmd.command.slice(1).toLowerCase().startsWith(query));
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
    if (level === usage?.effort) return;
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

  const modelLabel = usage?.model ?? "—";
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
              aria-haspopup="listbox"
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
                {CLAUDE_MODELS.map((m) => (
                  <button
                    key={m.name}
                    type="button"
                    role="option"
                    aria-selected={m.name === usage?.model}
                    onClick={() => selectModel(m.name)}
                    className="w-full text-left px-2 py-1.5 text-[12px] font-mono rounded-md cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
                    style={{
                      color: m.name === usage?.model ? C.accent : C.textPrimary,
                      backgroundColor: m.name === usage?.model ? C.accentSubtle : "transparent",
                    }}
                  >
                    {m.label}
                  </button>
                ))}
              </div>
            )}
          </div>

          {usage?.effort &&
            // A picker needs levels to offer. No capabilities, `canSwitchEffort:
            // false`, or an empty list all mean the same thing here: show the
            // level, don't pretend it can be changed.
            (effortSupported && effortLevels.length > 0 ? (
              <div className="relative">
                <button
                  type="button"
                  onClick={() => setEffortOpen((v) => !v)}
                  aria-haspopup="listbox"
                  aria-expanded={effortOpen}
                  aria-label="Effort-Stufe"
                  data-testid="effort-chip"
                  data-pending={pendingEffort != null}
                  disabled={pendingEffort != null}
                  className="inline-flex items-center gap-1 font-mono text-xs font-medium px-2 py-1 rounded-lg cursor-pointer transition-colors disabled:cursor-wait"
                  style={{
                    backgroundColor: effortOpen ? C.bgHover : "transparent",
                    color: pendingEffort != null ? C.textMuted : effortOpen ? C.textPrimary : C.textSecondary,
                    border: `1px solid ${C.border}`,
                    opacity: pendingEffort != null ? 0.6 : 1,
                  }}
                >
                  {usage.effort}
                  <ChevronDown
                    size={11}
                    className="transition-transform duration-150"
                    style={{ transform: effortOpen ? "rotate(180deg)" : undefined }}
                  />
                </button>
                {effortOpen && (
                  <div
                    role="listbox"
                    aria-label="Effort-Stufe"
                    data-testid="effort-menu"
                    className="absolute bottom-full left-0 mb-1.5 w-60 rounded-lg overflow-hidden z-20 p-1"
                    style={{
                      backgroundColor: C.bgElevated,
                      border: `1px solid ${C.border}`,
                      boxShadow: "var(--shadow-elevated)",
                    }}
                  >
                    {effortLevels.map((level) => {
                      const isCurrent = usage.effort === level;
                      const isPending = pendingEffort === level;
                      return (
                        <button
                          key={level}
                          type="button"
                          role="option"
                          aria-selected={isCurrent}
                          // The level verbatim, so callers (and tests) can
                          // address a row without parsing its label — "high"
                          // is otherwise a substring of "xhigh".
                          data-level={level}
                          onClick={() => selectEffort(level)}
                          className="w-full flex items-center gap-2 px-2 py-1.5 text-xs font-mono rounded-md cursor-pointer text-left transition-colors hover:bg-[var(--color-bg-hover)]"
                          style={{
                            color: isCurrent ? C.accent : C.textPrimary,
                            backgroundColor: isCurrent ? C.accentSubtle : "transparent",
                          }}
                        >
                          <span className="min-w-0 truncate">{level}</span>
                          {/* Per level, not one blanket line: low/medium/high/
                              xhigh rewrite the agent's persisted default, while
                              max/ultracode are session-only by CLI design
                              (agent_chat_input.py documents the split from
                              empirical testing). Which one the operator is
                              picking is the whole question the original "does
                              this outlive my session?" concern was about. */}
                          <span
                            className="ml-auto shrink-0 font-sans text-[10px]"
                            style={{ color: C.textMuted }}
                          >
                            {isSessionOnlyEffort(level) ? "nur diese Session" : "wird Standard"}
                          </span>
                          {/* "…" while the switch is in flight, a check only
                              once the transcript confirms it — the chip never
                              claims a level the agent hasn't reported. */}
                          {isPending ? (
                            <span className="shrink-0" style={{ color: C.textMuted }}>…</span>
                          ) : isCurrent ? (
                            <Check size={12} className="shrink-0" style={{ color: C.accent }} />
                          ) : (
                            <span className="shrink-0 w-3" aria-hidden="true" />
                          )}
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            ) : (
              <span
                data-testid="effort-chip-static"
                title="Effort lässt sich für diesen Agenten nicht über den Chat umstellen — seine Runtime hat kein steuerbares Terminal."
                className="font-mono text-xs font-medium px-2 py-1 rounded-lg"
                style={{ color: C.textMuted, border: `1px solid ${C.border}` }}
              >
                {usage.effort}
              </span>
            ))}

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

          {/* Circular controls, the app convention: a round button in a
              rounded field reads as "the one thing to press". */}
          <div className="ml-auto flex items-center gap-1.5">
            {sessionLive && isWorking && (
              <button
                type="button"
                onClick={onStop}
                aria-label="Stop"
                title="Unterbrechen (ESC)"
                data-testid="stop-button-prominent"
                className="animate-pulse inline-flex items-center justify-center w-9 h-9 md:w-8 md:h-8 rounded-full cursor-pointer"
                style={{
                  backgroundColor: C.accentSubtle,
                  color: C.textPrimary,
                  border: `1px solid ${C.borderAccent}`,
                }}
              >
                <Square size={12} fill={C.textPrimary} />
              </button>
            )}
            {sessionLive && !isWorking && (
              // Boss has no pane probe in v1 — "working" is often missed while
              // he's genuinely busy. A live session can always be interrupted,
              // even when we're not confident it's mid-task; this stays quiet
              // (unfilled icon, no pulse) so it doesn't compete with Send for
              // attention when the agent really is idle.
              <button
                type="button"
                onClick={onStop}
                aria-label="Stop"
                title="Unterbrechen (ESC)"
                data-testid="stop-button-quiet"
                className="inline-flex items-center justify-center w-8 h-8 md:w-7 md:h-7 rounded-full cursor-pointer transition-colors"
                style={{
                  backgroundColor: "transparent",
                  color: C.textMuted,
                  border: `1px solid ${C.borderSubtle}`,
                }}
              >
                <Square size={11} />
              </button>
            )}
            {!isWorking && (
              // Arrow-up-in-a-circle, the convention every current chat surface
              // uses (Codex, ChatGPT, Claude) — a paper plane reads as "send
              // mail", not "submit this turn". Empty input is a ghost outline
              // rather than a dimmed accent disc: the accent means "this is the
              // action", and there is no action until something is typed.
              <button
                type="button"
                onClick={send}
                aria-label="Senden"
                disabled={text.trim().length === 0}
                data-empty={text.trim().length === 0}
                className="inline-flex items-center justify-center w-9 h-9 md:w-8 md:h-8 rounded-full cursor-pointer disabled:cursor-not-allowed transition-colors"
                style={
                  text.trim().length === 0
                    ? {
                        backgroundColor: "transparent",
                        color: C.textDim,
                        border: `1px solid ${C.border}`,
                      }
                    : { backgroundColor: C.accent, color: C.onAccent }
                }
              >
                <ArrowUp size={16} strokeWidth={2.25} />
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

"use client";

import { useEffect, useRef, useState } from "react";
import { Square, ArrowUp, ChevronDown } from "lucide-react";
import { C, STATUS } from "@/lib/colors";
import type { StateEvent, UsageEvent } from "@/lib/chatTypes";
import { CLAUDE_MODELS, SLASH_COMMANDS, formatCompactTokens } from "@/lib/claudeCommands";

const MAX_ROWS = 8;
// Matches the textarea's own line-height (14px body × 1.5, rounded) so the
// auto-grow ceiling lands on a whole row instead of clipping one mid-glyph.
const LINE_HEIGHT_PX = 22;

// Desktop-app-style context ring — compact circular indicator instead of a
// bar, per the operator's ask to match the Claude Desktop app's look.
const RING_SIZE = 18;
const RING_STROKE = 2;
const RING_RADIUS = (RING_SIZE - RING_STROKE) / 2;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

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
export function Composer({ agentId, usage, state, onSend, onStop, sessionLive = true }: ComposerProps) {
  const [text, setText] = useState("");
  const [focused, setFocused] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [paletteIndex, setPaletteIndex] = useState(0);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const isWorking = state?.status === "working";

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
          Focus is drawn on the pill, not the textarea — the border plus the
          soft accent-alpha ring from DESIGN.md's input spec, never a glow. */}
      <div
        className="flex flex-col transition-colors"
        // One step above the ground, not below it: page and islands now share
        // bg-deep, so the old sunken-input look would have made the pill vanish
        // into the surface it sits on. A control lifts.
        style={{
          backgroundColor: C.bgSurface,
          borderRadius: "var(--radius-xl)",
          border: `1px solid ${focused ? `${C.accent}66` : C.borderActive}`,
          boxShadow: focused ? "0 0 0 3px rgba(235,232,222,0.10)" : undefined,
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
          className="w-full resize-none bg-transparent outline-none px-3.5 pt-3 text-[16px] md:text-[14px] leading-[1.5] placeholder:text-[var(--color-text-muted)]"
          style={{
            color: C.textPrimary,
            caretColor: C.accent,
            minHeight: LINE_HEIGHT_PX,
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
              className="inline-flex items-center gap-1 font-mono text-[11px] font-medium px-2 py-1 rounded-lg cursor-pointer transition-colors"
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

          {usage?.effort && (
            <span
              className="font-mono text-[11px] font-medium px-2 py-1 rounded-lg"
              style={{ color: C.textMuted, border: `1px solid ${C.border}` }}
            >
              {usage.effort}
            </span>
          )}

          {usage && pct != null && (
            <div
              data-testid="context-ring"
              role="progressbar"
              aria-valuenow={Math.round(pct)}
              aria-valuemin={0}
              aria-valuemax={100}
              data-threshold={ringThresholdValue}
              data-source={pctSource ?? undefined}
              title={ringTitle}
              className="flex items-center gap-1 shrink-0 pl-0.5"
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

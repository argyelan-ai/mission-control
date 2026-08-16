"use client";

import { useEffect, useRef, useState } from "react";
import { Command } from "cmdk";
import { Square, Send, ChevronDown } from "lucide-react";
import { C, STATUS } from "@/lib/colors";
import type { StateEvent, UsageEvent } from "@/lib/chatTypes";
import { CLAUDE_MODELS, SLASH_COMMANDS, formatCompactTokens } from "@/lib/claudeCommands";

const MAX_ROWS = 8;
const LINE_HEIGHT_PX = 18;

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
 * switcher + context meter, and a "/" command palette (cmdk, same styling
 * contract as the app's ⌘K palette). Enter sends, Shift+Enter inserts a
 * newline. Text reaches `onSend` raw — CRLF normalization already happens in
 * `api.chat.sendText` (B1), so this never touches it twice.
 */
export function Composer({ agentId, usage, state, onSend, onStop, sessionLive = true }: ComposerProps) {
  const [text, setText] = useState("");
  const [modelOpen, setModelOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const isWorking = state?.status === "working";

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    const maxHeight = MAX_ROWS * LINE_HEIGHT_PX;
    el.style.height = `${Math.min(el.scrollHeight, maxHeight)}px`;
  }, [text]);

  function send() {
    if (text.trim().length === 0) return;
    onSend(text);
    setText("");
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
      return;
    }
    if (e.key === "Escape" && paletteOpen) {
      e.preventDefault();
      setPaletteOpen(false);
    }
  }

  function handleChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    const value = e.target.value;
    setText(value);
    // Only a bare "/" as the very first character opens the palette — typed
    // fresh at position 0, not a slash appearing mid-text (e.g. a path).
    if (value === "/") {
      setPaletteOpen(true);
    } else if (paletteOpen && !value.startsWith("/")) {
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
      className="relative flex flex-col gap-2 px-3 py-2"
      style={{ borderTop: `1px solid ${C.border}`, backgroundColor: C.bgSurface }}
    >
      {paletteOpen && (
        <div
          className="absolute bottom-full left-3 mb-2 w-72 rounded-md overflow-hidden corner-ticks z-20"
          style={{
            backgroundColor: C.bgElevated,
            border: `1px solid ${C.border}`,
            boxShadow: "var(--shadow-elevated)",
          }}
        >
          <Command>
            <Command.List className="max-h-56 overflow-y-auto p-1.5">
              <Command.Empty
                className="py-4 text-center text-xs"
                style={{ color: C.textMuted }}
              >
                Keine Treffer
              </Command.Empty>
              {SLASH_COMMANDS.map((cmd) => (
                <Command.Item
                  key={cmd.command}
                  value={cmd.command}
                  onSelect={() => selectCommand(cmd.command)}
                  className="flex items-center gap-2 px-2 py-1.5 rounded-sm text-[13px] cursor-pointer font-mono data-[selected=true]:bg-[var(--color-accent-subtle)]"
                >
                  <span style={{ color: C.accent }}>{cmd.command}</span>
                  <span className="text-[10px] font-medium" style={{ color: C.textMuted }}>
                    {cmd.description}
                  </span>
                </Command.Item>
              ))}
            </Command.List>
          </Command>
        </div>
      )}

      <textarea
        ref={textareaRef}
        value={text}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        rows={1}
        placeholder="Nachricht an den Agenten…"
        className="w-full resize-none bg-transparent outline-none text-[13px] font-mono"
        style={{ color: C.textPrimary, maxHeight: MAX_ROWS * LINE_HEIGHT_PX }}
      />

      <div className="flex items-center gap-2">
        <div className="relative">
          <button
            type="button"
            onClick={() => setModelOpen((v) => !v)}
            className="inline-flex items-center gap-1 font-mono text-[10px] font-medium px-2 py-1 rounded-md"
            style={{
              backgroundColor: C.accentSubtle,
              color: C.textSecondary,
              border: `1px solid ${C.border}`,
            }}
          >
            {modelLabel}
            <ChevronDown size={11} />
          </button>
          {modelOpen && (
            <div
              className="absolute bottom-full left-0 mb-1 w-32 rounded-md overflow-hidden z-20"
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
                  onClick={() => selectModel(m.name)}
                  className="w-full text-left px-2 py-1.5 text-[12px] font-mono"
                  style={{ color: C.textPrimary }}
                >
                  {m.label}
                </button>
              ))}
            </div>
          )}
        </div>

        {usage?.effort && (
          <span
            className="font-mono text-[10px] font-medium px-2 py-1 rounded-md"
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
        )}

        <div className="ml-auto flex items-center gap-2">
          {sessionLive && isWorking && (
            <button
              type="button"
              onClick={onStop}
              aria-label="Stop"
              title="Unterbrechen (ESC)"
              data-testid="stop-button-prominent"
              className="animate-pulse inline-flex items-center justify-center w-7 h-7 rounded-md"
              style={{
                backgroundColor: C.accentSubtle,
                color: C.textPrimary,
                border: `1px solid ${C.border}`,
              }}
            >
              <Square size={13} fill={C.textPrimary} />
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
              className="inline-flex items-center justify-center w-6 h-6 rounded-md"
              style={{
                backgroundColor: "transparent",
                color: C.textDim,
                border: `1px solid ${C.borderSubtle}`,
              }}
            >
              <Square size={11} />
            </button>
          )}
          {!isWorking && (
            <button
              type="button"
              onClick={send}
              aria-label="Senden"
              disabled={text.trim().length === 0}
              className="inline-flex items-center justify-center w-7 h-7 rounded-md disabled:opacity-40"
              style={{
                backgroundColor: C.accent,
                color: C.onAccent,
              }}
            >
              <Send size={13} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

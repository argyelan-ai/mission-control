"use client";

import { useEffect, useRef, useState } from "react";
import { Command } from "cmdk";
import { Square, Send, ChevronDown } from "lucide-react";
import { C } from "@/lib/colors";
import type { StateEvent, UsageEvent } from "@/lib/chatTypes";
import { CLAUDE_MODELS, SLASH_COMMANDS, contextWindow } from "@/lib/claudeCommands";

const MAX_ROWS = 8;
const LINE_HEIGHT_PX = 18;

interface ComposerProps {
  agentId: string;
  /** Transcript truth — never an optimistic guess of what model is active. */
  usage: UsageEvent | null;
  state: StateEvent | null;
  onSend: (text: string) => void;
  onStop: () => void;
}

/**
 * The input row of the Sessions Chat view: auto-growing textarea, model
 * switcher + context meter, and a "/" command palette (cmdk, same styling
 * contract as the app's ⌘K palette). Enter sends, Shift+Enter inserts a
 * newline. Text reaches `onSend` raw — CRLF normalization already happens in
 * `api.chat.sendText` (B1), so this never touches it twice.
 */
export function Composer({ agentId, usage, state, onSend, onStop }: ComposerProps) {
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
  const win = contextWindow(usage?.model);
  const fillRatio = usage ? Math.min(usage.inputTokens / win, 1) : 0;

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

        {usage && (
          <div
            data-testid="context-meter"
            className="h-1.5 rounded-full overflow-hidden shrink-0"
            style={{ width: 64, backgroundColor: C.bgHover }}
            title={`${usage.inputTokens.toLocaleString("en-US")} / ${win.toLocaleString("en-US")} Tokens`}
          >
            <div
              data-testid="context-meter-fill"
              className="h-full"
              style={{
                width: `${fillRatio * 100}%`,
                backgroundColor: fillRatio > 0.85 ? C.warning : C.accent,
              }}
            />
          </div>
        )}

        <div className="ml-auto flex items-center gap-2">
          {isWorking ? (
            <button
              type="button"
              onClick={onStop}
              aria-label="Stop"
              className="inline-flex items-center justify-center w-7 h-7 rounded-md"
              style={{
                backgroundColor: C.accentSubtle,
                color: C.textPrimary,
                border: `1px solid ${C.border}`,
              }}
            >
              <Square size={13} fill={C.textPrimary} />
            </button>
          ) : (
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

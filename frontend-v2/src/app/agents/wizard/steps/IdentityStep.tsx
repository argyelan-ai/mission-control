"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import type { WizardStepProps } from "../types";
import { wizardInputClass, wizardInputStyle, wizardLabelClass, wizardSelectStyle } from "../shared";

export function IdentityStep({ state, update, boards }: WizardStepProps) {
  // Debounce the preview so we don't POST on every keystroke.
  const [debounced, setDebounced] = useState({
    name: state.name,
    emoji: state.emoji,
    role: state.role,
    soulMd: state.soulMd,
  });
  useEffect(() => {
    const t = setTimeout(
      () => setDebounced({ name: state.name, emoji: state.emoji, role: state.role, soulMd: state.soulMd }),
      400
    );
    return () => clearTimeout(t);
  }, [state.name, state.emoji, state.role, state.soulMd]);

  const { data: preview, isFetching } = useQuery({
    queryKey: ["soul-preview", debounced, state.boardId, state.isBoardLead, state.scopes],
    queryFn: () =>
      api.agents.previewSoul({
        name: debounced.name || "Agent",
        emoji: debounced.emoji || undefined,
        role: debounced.role || undefined,
        soul_md: debounced.soulMd ?? undefined,
        board_id: state.boardId || undefined,
        is_board_lead: state.isBoardLead,
        scopes: state.scopes,
      }),
    enabled: debounced.name.trim().length > 0,
    staleTime: 5_000,
    retry: false,
  });

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
      {/* Left: form */}
      <div className="space-y-4">
        <div>
          <label className={wizardLabelClass}>Name *</label>
          <input
            type="text"
            value={state.name}
            onChange={(e) => update({ name: e.target.value })}
            placeholder="z.B. Cody"
            className={wizardInputClass}
            style={wizardInputStyle}
            autoFocus
          />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={wizardLabelClass}>Emoji</label>
            <input
              type="text"
              value={state.emoji}
              onChange={(e) => update({ emoji: e.target.value })}
              placeholder="🤖"
              className={wizardInputClass}
              style={wizardInputStyle}
            />
          </div>
          <div>
            <label className={wizardLabelClass}>Rolle</label>
            <input
              type="text"
              value={state.role}
              onChange={(e) => update({ role: e.target.value })}
              placeholder="Developer"
              className={wizardInputClass}
              style={wizardInputStyle}
            />
          </div>
        </div>
        {boards.length > 0 && (
          <div>
            <label className={wizardLabelClass}>Board</label>
            <select
              value={state.boardId}
              onChange={(e) => update({ boardId: e.target.value })}
              className={`${wizardInputClass} cursor-pointer`}
              style={wizardSelectStyle}
            >
              <option value="">Kein Board</option>
              {boards.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name}
                </option>
              ))}
            </select>
          </div>
        )}
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={state.isBoardLead}
            onChange={(e) => update({ isBoardLead: e.target.checked })}
            className="cursor-pointer accent-[var(--color-accent)]"
          />
          <span className="text-[12px] text-[var(--color-text-secondary)]">
            Board Lead (Orchestrator — erhält alle Rechte)
          </span>
        </label>
      </div>

      {/* Right: live SOUL preview */}
      <div>
        <label className={wizardLabelClass}>
          Persona-Vorschau {isFetching && <span className="normal-case">· lädt…</span>}
        </label>
        <div
          className="rounded-xl p-3 text-[11px] font-mono whitespace-pre-wrap leading-relaxed h-[280px] overflow-y-auto"
          style={{
            backgroundColor: "rgba(255,255,255,0.02)",
            border: `1px solid ${C.borderSubtle}`,
            color: "var(--color-text-secondary)",
          }}
        >
          {preview?.soul_md ?? "Name eingeben, um die generierte Persona zu sehen…"}
        </div>
      </div>
    </div>
  );
}

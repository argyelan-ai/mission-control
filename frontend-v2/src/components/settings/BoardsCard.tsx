"use client";

/**
 * Boards — Settings/Profile. "Create board" lives here while there is only
 * one board (operator decision 12.10.2026, variant B): the sidebar keeps
 * showing the board name as plain text with no switcher or lone "+" to
 * create one, so this quiet card is where the capability is not lost. With
 * two or more boards, the sidebar's own switcher already carries "+ New
 * board" in its dropdown, so this card steps back (K3 — the same action
 * is not offered twice).
 */
import { useState, useRef, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { Plus, Check } from "lucide-react";
import { api } from "@/lib/api";
import { slugify } from "@/lib/utils";
import { canSwitchBoards } from "@/lib/boards";
import type { Board } from "@/lib/types";
import { C, WORKSPACE_COLORS as BOARD_COLORS } from "@/lib/colors";
import { EntityIcon, ENTITY_ICON_KEYS } from "@/components/shared/EntityIcon";

const BOARD_ICONS = ENTITY_ICON_KEYS.slice(0, 12);

const cardStyle = {
  background: C.bgSurface,
  border: `1px solid ${C.border}`,
  borderRadius: 12,
} as const;

export function BoardsCard() {
  const t = useTranslations("settingsProfile");
  const tShell = useTranslations("shell");
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [color, setColor] = useState(BOARD_COLORS[0]);
  const [icon, setIcon] = useState(BOARD_ICONS[0]);
  const inputRef = useRef<HTMLInputElement>(null);

  const { data } = useQuery<Board[]>({ queryKey: ["boards"], queryFn: api.boards.list });
  const boards = data ?? [];

  useEffect(() => {
    if (creating && inputRef.current) inputRef.current.focus();
  }, [creating]);

  const createMutation = useMutation({
    mutationFn: (payload: Partial<Board>) => api.boards.create(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["boards"] });
      setCreating(false);
      setName("");
    },
  });

  // The sidebar switcher already offers "+ New board" once there is
  // something to switch between — this card would just be the same action
  // a second time (K3).
  if (canSwitchBoards(boards)) return null;

  function handleCreate() {
    if (!name.trim()) return;
    createMutation.mutate({ name: name.trim(), slug: slugify(name.trim()), color, icon });
  }

  return (
    <div className="mc-card p-6 space-y-4 mt-6" style={cardStyle} data-testid="settings-boards-card">
      <div>
        <h3 className="text-sm font-semibold" style={{ color: C.textPrimary }}>
          {t("boards.title")}
        </h3>
        <p className="text-xs mt-1" style={{ color: C.textMuted }}>
          {t("boards.description")}
        </p>
      </div>

      {!creating ? (
        <button
          type="button"
          onClick={() => setCreating(true)}
          className="flex items-center gap-2 px-3 py-2 rounded-lg text-xs font-medium cursor-pointer transition-all"
          style={{ color: C.accent, border: `1px solid ${C.borderAccent}`, backgroundColor: C.accentSubtle }}
        >
          <Plus size={12} aria-hidden />
          {tShell("newBoard")}
        </button>
      ) : (
        <div className="flex flex-col gap-3" data-testid="settings-boards-create-form">
          <input
            ref={inputRef}
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") handleCreate();
              if (e.key === "Escape") setCreating(false);
            }}
            placeholder={tShell("boardNamePlaceholder")}
            aria-label={tShell("boardNamePlaceholder")}
            className="w-full px-3 py-2 rounded-lg text-sm outline-none"
            style={{ background: C.bgDeep, border: `1px solid ${C.border}`, color: C.textPrimary }}
          />
          <div className="flex gap-2 flex-wrap">
            {BOARD_COLORS.slice(0, 8).map((c) => (
              <button
                key={c}
                type="button"
                onClick={() => setColor(c)}
                aria-label={c}
                aria-pressed={color === c}
                style={{
                  width: 18,
                  height: 18,
                  borderRadius: "var(--radius-full)",
                  backgroundColor: c,
                  border: color === c ? `2px solid ${C.textPrimary}` : "1px solid transparent",
                }}
              />
            ))}
          </div>
          <div className="flex gap-2 flex-wrap">
            {BOARD_ICONS.slice(0, 8).map((k) => (
              <button
                key={k}
                type="button"
                onClick={() => setIcon(k)}
                aria-label={k}
                aria-pressed={icon === k}
                className="grid place-items-center"
                style={{
                  width: 26,
                  height: 26,
                  borderRadius: "var(--radius-sm)",
                  border: `1px solid ${icon === k ? C.borderAccent : C.border}`,
                  color: icon === k ? C.accent : C.textMuted,
                }}
              >
                <EntityIcon value={k} size={14} />
              </button>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={handleCreate}
              disabled={!name.trim() || createMutation.isPending}
              className="px-3 py-2 rounded-lg text-xs font-semibold cursor-pointer disabled:opacity-40"
              style={{ backgroundColor: C.accent, color: C.onAccent }}
            >
              {createMutation.isPending ? (
                tShell("creating")
              ) : (
                <span className="flex items-center gap-2">
                  <Check size={12} aria-hidden />
                  {tShell("createBoard")}
                </span>
              )}
            </button>
            <button
              type="button"
              onClick={() => setCreating(false)}
              className="px-3 py-2 rounded-lg text-xs cursor-pointer"
              style={{ color: C.textMuted }}
            >
              {tShell("close")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

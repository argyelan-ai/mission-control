"use client";

import { useState, useRef, useEffect } from "react";
import { createPortal } from "react-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { motion, AnimatePresence } from "framer-motion";
import { useTranslations } from "next-intl";
import { Plus, Check } from "lucide-react";
import { useAppStore } from "@/lib/store";
import { api } from "@/lib/api";
import { slugify } from "@/lib/utils";
import type { Board } from "@/lib/types";
import { P2, WORKSPACE_COLORS as BOARD_COLORS } from "@/lib/colors";
import { EntityIcon, ENTITY_ICON_KEYS } from "@/components/shared/EntityIcon";

const BOARD_ICONS = ENTITY_ICON_KEYS.slice(0, 12);
const MONO = { fontFamily: "var(--font-p2-mono)" };

/**
 * BoardPicker — Shell v4 zone 1. Replaces the 48px WorkspaceSwitcher rail:
 * same boards, same create flow, one row instead of a column.
 */
export default function BoardPicker({ collapsed = false }: { collapsed?: boolean }) {
  const t = useTranslations("shell");
  const { boards, activeBoardId, setActiveBoardId, setBoards } = useAppStore();
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [color, setColor] = useState(BOARD_COLORS[0]);
  const [icon, setIcon] = useState(BOARD_ICONS[0]);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [anchor, setAnchor] = useState<{ top: number; left: number; width: number } | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const queryClient = useQueryClient();

  const { data } = useQuery({ queryKey: ["boards"], queryFn: api.boards.list });

  useEffect(() => {
    if (data && data !== boards) {
      setBoards(data);
      if (!activeBoardId && data.length > 0) setActiveBoardId(data[0].id);
    }
  }, [data, boards, activeBoardId, setBoards, setActiveBoardId]);

  const createMutation = useMutation({
    mutationFn: (payload: Partial<Board>) => api.boards.create(payload),
    onSuccess: (newBoard) => {
      queryClient.invalidateQueries({ queryKey: ["boards"] });
      setActiveBoardId(newBoard.id);
      setCreating(false);
      setOpen(false);
      setName("");
    },
  });

  useEffect(() => {
    if (creating && inputRef.current) inputRef.current.focus();
  }, [creating]);

  // Close on outside click / Escape
  useEffect(() => {
    if (!open) return;
    function onClick(e: MouseEvent) {
      const t = e.target as Node;
      const inside =
        rootRef.current?.contains(t) || menuRef.current?.contains(t);
      if (!inside) {
        setOpen(false);
        setCreating(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        setOpen(false);
        setCreating(false);
      }
    }
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // The sidebar clips its children (overflow:hidden keeps the rounded card and
  // the scrolling nav honest), so the menu is rendered into the body and
  // positioned from the trigger's rect instead of flowing inside the column.
  useEffect(() => {
    if (!open) return;
    const place = () => {
      const r = triggerRef.current?.getBoundingClientRect();
      if (!r) return;
      setAnchor(
        collapsed
          ? { top: r.top, left: r.right + 8, width: 210 }
          : { top: r.bottom + 6, left: r.left, width: r.width }
      );
    };
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [open, collapsed]);

  const displayBoards = data ?? boards;
  const active = displayBoards.find((b) => b.id === activeBoardId) ?? displayBoards[0];
  const activeColor = active?.color ?? P2.amb;

  function handleCreate() {
    if (!name.trim()) return;
    createMutation.mutate({ name: name.trim(), slug: slugify(name.trim()), color, icon });
  }

  return (
    <div ref={rootRef} className="relative shrink-0">
      <button
        ref={triggerRef}
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        title={collapsed ? (active?.name ?? t("boardPicker")) : undefined}
        className="w-full flex items-center gap-2.5 cursor-pointer"
        style={{
          height: collapsed ? "38px" : "38px",
          padding: collapsed ? 0 : "0 12px",
          justifyContent: collapsed ? "center" : "flex-start",
          borderRadius: "12px",
          backgroundColor: collapsed ? "transparent" : "var(--color-p2-pan2)",
          border: `1px solid ${open ? "var(--color-p2-amb-d)" : collapsed ? "transparent" : "var(--color-p2-line2)"}`,
          ...MONO,
          fontSize: "12px",
          color: "var(--color-p2-txt)",
        }}
      >
        <span
          className="shrink-0 flex items-center justify-center"
          style={{
            width: collapsed ? 20 : 10,
            height: collapsed ? 20 : 10,
            borderRadius: "999px",
            backgroundColor: collapsed ? "transparent" : activeColor,
            color: activeColor,
          }}
        >
          {collapsed && (active?.icon ? <EntityIcon value={active.icon} size={16} /> : null)}
        </span>
        {!collapsed && (
          <>
            <span className="truncate">{active?.name ?? t("boardPicker")}</span>
            <span
              className="ml-auto shrink-0"
              style={{ color: open ? "var(--color-p2-amb)" : "var(--color-p2-faint)", fontSize: "10px" }}
            >
              {open ? "▴" : "▾"}
            </span>
          </>
        )}
      </button>

      {createPortal(
        <AnimatePresence>
          {open && anchor && (
            <motion.div
              ref={menuRef}
              initial={{ opacity: 0, y: -6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -6 }}
              transition={{ duration: 0.16, ease: [0.16, 1, 0.3, 1] }}
              role="listbox"
              className="fixed z-[60] p-1.5"
              style={{
                top: anchor.top,
                left: anchor.left,
                width: anchor.width,
                backgroundColor: "var(--color-p2-pan2)",
                border: "1px solid var(--color-p2-line)",
                borderRadius: "12px",
                boxShadow: "var(--shadow-elevated)",
              }}
            >
            {displayBoards.map((board) => {
              const isActive = board.id === (active?.id ?? null);
              return (
                <button
                  key={board.id}
                  role="option"
                  aria-selected={isActive}
                  onClick={() => {
                    setActiveBoardId(board.id);
                    setOpen(false);
                  }}
                  className="w-full flex items-center gap-2.5 cursor-pointer"
                  style={{
                    height: "32px",
                    padding: "0 10px",
                    borderRadius: "9px",
                    ...MONO,
                    fontSize: "11.5px",
                    fontWeight: isActive ? 700 : 400,
                    color: isActive ? "var(--color-p2-txt)" : "var(--color-p2-dim)",
                    backgroundColor: isActive ? "var(--color-accent-subtle)" : "transparent",
                  }}
                  onMouseEnter={(e) => {
                    if (!isActive) (e.currentTarget as HTMLElement).style.backgroundColor = "var(--color-p2-pan)";
                  }}
                  onMouseLeave={(e) => {
                    if (!isActive) (e.currentTarget as HTMLElement).style.backgroundColor = "transparent";
                  }}
                >
                  <span
                    className="shrink-0"
                    style={{
                      width: 9,
                      height: 9,
                      borderRadius: "999px",
                      backgroundColor: board.color ?? P2.amb,
                    }}
                  />
                  <span className="truncate">{board.name}</span>
                  {isActive && <Check size={12} className="ml-auto shrink-0" />}
                </button>
              );
            })}

            <div style={{ height: 1, backgroundColor: "var(--color-p2-line2)", margin: "5px 4px" }} />

            {!creating ? (
              <button
                onClick={() => setCreating(true)}
                className="w-full flex items-center gap-2.5 cursor-pointer"
                style={{
                  height: "32px",
                  padding: "0 10px",
                  borderRadius: "9px",
                  ...MONO,
                  fontSize: "11.5px",
                  color: "var(--color-p2-faint)",
                }}
                onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.color = "var(--color-p2-txt)")}
                onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.color = "var(--color-p2-faint)")}
              >
                <Plus size={13} className="shrink-0" />
                {t("newBoard")}
              </button>
            ) : (
              <div className="p-1.5 flex flex-col gap-2">
                <input
                  ref={inputRef}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") handleCreate();
                    if (e.key === "Escape") setCreating(false);
                  }}
                  placeholder={t("boardNamePlaceholder")}
                  className="w-full px-2.5 py-1.5 outline-none"
                  style={{
                    ...MONO,
                    fontSize: "11.5px",
                    borderRadius: "8px",
                    backgroundColor: "var(--color-p2-inset)",
                    border: "1px solid var(--color-p2-line)",
                    color: "var(--color-p2-txt)",
                  }}
                />
                <div className="flex gap-1.5 flex-wrap">
                  {BOARD_COLORS.slice(0, 8).map((c) => (
                    <button
                      key={c}
                      onClick={() => setColor(c)}
                      aria-label={c}
                      style={{
                        width: 16,
                        height: 16,
                        borderRadius: "999px",
                        backgroundColor: c,
                        border: color === c ? "2px solid var(--color-p2-txt)" : "1px solid transparent",
                      }}
                    />
                  ))}
                </div>
                <div className="flex gap-1.5 flex-wrap">
                  {BOARD_ICONS.slice(0, 8).map((k) => (
                    <button
                      key={k}
                      onClick={() => setIcon(k)}
                      aria-label={k}
                      className="grid place-items-center"
                      style={{
                        width: 22,
                        height: 22,
                        borderRadius: "7px",
                        border: `1px solid ${icon === k ? "var(--color-p2-amb-d)" : "var(--color-p2-line2)"}`,
                        color: icon === k ? "var(--color-p2-amb)" : "var(--color-p2-dim)",
                      }}
                    >
                      <EntityIcon value={k} size={13} />
                    </button>
                  ))}
                </div>
                <button
                  onClick={handleCreate}
                  disabled={!name.trim() || createMutation.isPending}
                  className="w-full cursor-pointer disabled:opacity-40"
                  style={{
                    height: 30,
                    borderRadius: "9px",
                    backgroundColor: "var(--color-p2-amb)",
                    color: "var(--color-p2-inv)",
                    fontFamily: "var(--font-p2-display)",
                    fontWeight: 700,
                    fontSize: "10.5px",
                    letterSpacing: "0.06em",
                  }}
                >
                  {createMutation.isPending ? t("creating") : t("createBoard")}
                </button>
              </div>
            )}
            </motion.div>
          )}
        </AnimatePresence>,
        document.body
      )}
    </div>
  );
}

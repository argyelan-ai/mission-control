"use client";

import { useState, useRef, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { motion, AnimatePresence } from "framer-motion";
import { Plus, X } from "lucide-react";
import { useAppStore } from "@/lib/store";
import { api } from "@/lib/api";
import { cn, slugify } from "@/lib/utils";
import type { Board } from "@/lib/types";
import { P2, WORKSPACE_COLORS as BOARD_COLORS } from "@/lib/colors";
import { EntityIcon, ENTITY_ICON_KEYS } from "@/components/shared/EntityIcon";

const BOARD_ICONS = ENTITY_ICON_KEYS.slice(0, 12);

const MONO = { fontFamily: "var(--font-p2-mono)" };

export default function WorkspaceSwitcher() {
  const { boards, activeBoardId, setActiveBoardId, setBoards } = useAppStore();
  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [color, setColor] = useState(BOARD_COLORS[0]);
  const [icon, setIcon] = useState(BOARD_ICONS[0]);
  const inputRef = useRef<HTMLInputElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const queryClient = useQueryClient();

  const { data } = useQuery({
    queryKey: ["boards"],
    queryFn: api.boards.list,
  });

  useEffect(() => {
    if (data && data !== boards) {
      setBoards(data);
      if (!activeBoardId && data.length > 0) {
        setActiveBoardId(data[0].id);
      }
    }
  }, [data, boards, activeBoardId, setBoards, setActiveBoardId]);

  const createMutation = useMutation({
    mutationFn: (payload: Partial<Board>) => api.boards.create(payload),
    onSuccess: (newBoard) => {
      queryClient.invalidateQueries({ queryKey: ["boards"] });
      setActiveBoardId(newBoard.id);
      setShowCreate(false);
      setName("");
    },
  });

  useEffect(() => {
    if (showCreate && inputRef.current) inputRef.current.focus();
  }, [showCreate]);

  useEffect(() => {
    if (!showCreate) return;
    function handleClick(e: MouseEvent) {
      if (popoverRef.current && !popoverRef.current.contains(e.target as Node)) {
        setShowCreate(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [showCreate]);

  function handleCreate() {
    if (!name.trim()) return;
    createMutation.mutate({
      name: name.trim(),
      slug: slugify(name.trim()),
      color,
      icon,
    });
  }

  const displayBoards = data ?? boards;

  return (
    <div
      className="relative flex flex-col items-center py-3 gap-2.5"
      style={{
        width: "48px",
        minWidth: "48px",
        backgroundColor: "var(--color-p2-bg)",
        borderRight: "1px solid var(--color-p2-line2)",
      }}
    >
      {displayBoards.map((board) => {
        const isActive = board.id === activeBoardId;
        const boardColor = board.color ?? P2.amb;
        return (
          <motion.button
            key={board.id}
            onClick={() => setActiveBoardId(board.id)}
            whileHover={{ scale: 1.08 }}
            whileTap={{ scale: 0.92 }}
            title={board.name}
            className={cn(
              "relative w-11 h-11 rounded-md flex items-center justify-center text-base transition-all cursor-pointer",
              isActive ? "" : "opacity-50 hover:opacity-100"
            )}
            style={{
              background: isActive
                ? `${boardColor}44`
                : `${boardColor}22`,
              border: isActive
                ? `1.5px solid ${boardColor}88`
                : `1px solid ${boardColor}33`,
            }}
          >
            <span className="relative z-10">
              {board.icon
                ? <EntityIcon value={board.icon} size={16} />
                : (board.name[0]?.toUpperCase() ?? "B")}
            </span>

            {isActive && (
              <motion.div
                layoutId="workspace-indicator"
                className="absolute -left-[5px] w-[3px] h-5"
                style={{
                  background: boardColor,
                }}
              />
            )}
          </motion.button>
        );
      })}

      {/* Add board button */}
      <motion.button
        onClick={() => setShowCreate(true)}
        whileHover={{ scale: 1.08 }}
        whileTap={{ scale: 0.92 }}
        title="New board"
        className="w-11 h-11 rounded-md flex items-center justify-center opacity-30 hover:opacity-80 transition-all cursor-pointer mt-1"
        style={{
          background: "var(--color-p2-inset)",
          border: "1.5px dashed var(--color-p2-line)",
          color: "var(--color-p2-dim)",
        }}
      >
        <Plus size={15} />
      </motion.button>

      {/* Create popover — Modal, darf Schatten tragen */}
      <AnimatePresence>
        {showCreate && (
          <motion.div
            ref={popoverRef}
            initial={{ opacity: 0, x: -8, scale: 0.95 }}
            animate={{ opacity: 1, x: 0, scale: 1 }}
            exit={{ opacity: 0, x: -8, scale: 0.95 }}
            transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
            className="absolute left-14 bottom-4 z-50 w-64"
            style={{
              backgroundColor: "var(--color-p2-pan)",
              border: "1px solid var(--color-p2-line)",
              boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
            }}
          >
            {/* Akzent-Kante oben — Signatur */}
            <div className="h-[2px] w-full" style={{ backgroundColor: P2.amb }} />

            <div
              className="flex items-center justify-between px-4 py-3"
              style={{ borderBottom: "1px solid var(--color-p2-line2)" }}
            >
              <span
                style={{
                  fontFamily: "var(--font-p2-display)",
                  fontWeight: 700,
                  fontSize: "11px",
                  letterSpacing: "0.14em",
                  color: "var(--color-p2-txt)",
                }}
              >
                NEW BOARD
              </span>
              <button
                onClick={() => setShowCreate(false)}
                className="transition-colors cursor-pointer"
                style={{ color: "var(--color-p2-dim)" }}
                aria-label="Close"
              >
                <X size={14} />
              </button>
            </div>

            <div className="p-4 space-y-3">
              <input
                ref={inputRef}
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleCreate()}
                placeholder="Board name"
                className="w-full px-3 py-2 text-sm transition-all"
                style={{
                  ...MONO,
                  backgroundColor: "var(--color-p2-inset)",
                  border: "1px solid var(--color-p2-line)",
                  color: "var(--color-p2-txt)",
                  outline: "none",
                }}
                onFocus={(e) => { e.target.style.borderColor = P2.ambD; }}
                onBlur={(e) => { e.target.style.borderColor = "var(--color-p2-line)"; }}
              />

              <div>
                <div className="mb-1.5" style={{ ...MONO, fontSize: "10px", letterSpacing: "0.14em", color: "var(--color-p2-dim)" }}>
                  ICON
                </div>
                <div className="flex flex-wrap gap-1">
                  {BOARD_ICONS.map((i) => (
                    <button
                      key={i}
                      onClick={() => setIcon(i)}
                      className={cn(
                        "w-7 h-7 flex items-center justify-center text-sm transition-all cursor-pointer",
                        icon === i ? "" : "hover:opacity-80"
                      )}
                      style={
                        icon === i
                          ? { outline: `2px solid ${P2.amb}`, backgroundColor: "var(--color-p2-pan2)" }
                          : { backgroundColor: "var(--color-p2-inset)" }
                      }
                    >
                      {i}
                    </button>
                  ))}
                </div>
              </div>

              <div>
                <div className="mb-1.5" style={{ ...MONO, fontSize: "10px", letterSpacing: "0.14em", color: "var(--color-p2-dim)" }}>
                  COLOR
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {BOARD_COLORS.map((c) => (
                    <button
                      key={c}
                      onClick={() => setColor(c)}
                      className={cn(
                        "w-6 h-6 rounded-full transition-all cursor-pointer",
                        color === c ? "scale-110" : "hover:scale-110"
                      )}
                      style={{
                        backgroundColor: c,
                        outlineOffset: "2px",
                        outline: color === c ? `2px solid ${c}` : undefined,
                      }}
                    />
                  ))}
                </div>
              </div>

              <button
                onClick={handleCreate}
                disabled={!name.trim() || createMutation.isPending}
                className="w-full px-4 py-2 transition-all cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed hover:brightness-110"
                style={{
                  ...MONO,
                  fontWeight: 700,
                  fontSize: "11px",
                  letterSpacing: "0.12em",
                  minHeight: "44px",
                  background: P2.amb,
                  color: P2.inv,
                }}
              >
                {createMutation.isPending ? "CREATING…" : "CREATE BOARD"}
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

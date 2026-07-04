"use client";

import { useState, useRef, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { motion, AnimatePresence } from "framer-motion";
import { Plus, X } from "lucide-react";
import { useAppStore } from "@/lib/store";
import { api } from "@/lib/api";
import { cn, slugify } from "@/lib/utils";
import type { Board } from "@/lib/types";
import { C, WORKSPACE_COLORS as BOARD_COLORS } from "@/lib/colors";

const BOARD_ICONS = ["🚀", "⚡", "🛠", "🎯", "🧠", "💡", "🔥", "📦", "🌍", "🤖"];

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
      className="relative flex flex-col items-center py-3 gap-2.5 border-r"
      style={{
        width: "48px",
        minWidth: "48px",
        backgroundColor: C.bgDeep,
        borderColor: C.border,
      }}
    >
      {displayBoards.map((board) => {
        const isActive = board.id === activeBoardId;
        const boardColor = board.color ?? C.accent;
        return (
          <motion.button
            key={board.id}
            onClick={() => setActiveBoardId(board.id)}
            whileHover={{ scale: 1.08 }}
            whileTap={{ scale: 0.92 }}
            title={board.name}
            className={cn(
              "relative w-11 h-11 rounded-xl flex items-center justify-center text-base transition-all cursor-pointer",
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
            <span className="relative z-10 drop-shadow-sm">
              {board.icon ?? board.name[0]?.toUpperCase() ?? "B"}
            </span>

            {isActive && (
              <motion.div
                layoutId="workspace-indicator"
                className="absolute -left-[5px] w-[3px] h-5 rounded-r-full"
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
        title="Neues Board"
        className="w-11 h-11 rounded-xl flex items-center justify-center opacity-30 hover:opacity-80 transition-all cursor-pointer mt-1"
        style={{
          background: C.borderSubtle,
          border: `1.5px dashed ${C.borderActive}`,
          color: C.textSecondary,
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
            className="absolute left-14 bottom-4 z-50 w-64 rounded-2xl"
            style={{
              backgroundColor: C.bgBase,
              border: `1px solid ${C.border}`,
              boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
            }}
          >
            {/* Top edge highlight */}
            <div
              className="absolute top-0 left-0 right-0 h-px rounded-t-2xl"
              style={{ background: `linear-gradient(90deg, transparent, ${C.borderActive}, transparent)` }}
            />

            <div
              className="flex items-center justify-between px-4 py-3 border-b"
              style={{ borderColor: C.border }}
            >
              <span className="text-sm font-semibold" style={{ color: C.textPrimary }}>
                Neues Board
              </span>
              <button
                onClick={() => setShowCreate(false)}
                className="transition-colors cursor-pointer"
                style={{ color: C.textSecondary }}
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
                placeholder="Board Name"
                className="w-full rounded-lg px-3 py-2 text-sm transition-all"
                style={{
                  backgroundColor: C.bgDeep,
                  border: `1px solid ${C.border}`,
                  color: C.textPrimary,
                  outline: "none",
                }}
                onFocus={(e) => { e.target.style.borderColor = C.borderAccent; }}
                onBlur={(e) => { e.target.style.borderColor = C.border; }}
              />

              <div>
                <div className="text-xs mb-1.5" style={{ color: C.textSecondary }}>Icon</div>
                <div className="flex flex-wrap gap-1">
                  {BOARD_ICONS.map((i) => (
                    <button
                      key={i}
                      onClick={() => setIcon(i)}
                      className={cn(
                        "w-7 h-7 rounded-md flex items-center justify-center text-sm transition-all cursor-pointer",
                        icon === i
                          ? "ring-2"
                          : "hover:opacity-80"
                      )}
                      style={
                        icon === i
                          ? { outline: `2px solid ${C.accent}`, backgroundColor: C.accentSubtle }
                          : { backgroundColor: C.borderSubtle }
                      }
                    >
                      {i}
                    </button>
                  ))}
                </div>
              </div>

              <div>
                <div className="text-xs mb-1.5" style={{ color: C.textSecondary }}>Farbe</div>
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
                className="w-full font-medium text-sm rounded-lg px-4 py-2 transition-all cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
                style={{
                  background: `linear-gradient(135deg, ${C.accent}, ${C.accentHover})`,
                  color: C.textPrimary,
                }}
              >
                {createMutation.isPending ? "Erstelle…" : "Board erstellen"}
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

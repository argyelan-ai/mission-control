"use client";

/**
 * Settings on the phone — who is signed in, which board is active, log out.
 *
 * Until variant B of the phone menu (2026-10-02) these three lived in the old
 * menu drawer. The ⊕ sheet now only starts things and leads to pages, so they
 * moved here, to the top of Settings (concept mobile-menu-v3: "Board" area,
 * "Log out" in Settings). Phone only (`md:hidden`): on the desktop the
 * sidebar keeps its board picker and account footer, unchanged.
 */
import { useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useQuery } from "@tanstack/react-query";
import { Check, ChevronDown, LogOut } from "lucide-react";
import { api, clearToken } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import type { Board } from "@/lib/types";
import { C } from "@/lib/colors";
import { EntityIcon } from "@/components/shared/EntityIcon";

const rowClass =
  "w-full flex items-center gap-4 min-h-12 px-1 rounded-lg text-left cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]";

function BoardMark({ board }: { board: Board | null }) {
  if (board?.icon) {
    return (
      <span className="shrink-0 w-5 flex justify-center leading-none" aria-hidden>
        <EntityIcon value={board.icon} size={16} />
      </span>
    );
  }
  return (
    <span className="shrink-0 w-5 flex justify-center" aria-hidden>
      {/* Board colours are a DESIGN.md exception (operator decision 23.09.). */}
      <span className="w-2.5 h-2.5 rounded-full" style={{ background: board?.color ?? C.textSecondary }} />
    </span>
  );
}

export function PhoneAccountPanel() {
  const t = useTranslations("settings.phoneAccount");
  const tNav = useTranslations("nav");
  const router = useRouter();
  const { currentUser, activeBoardId, setActiveBoardId } = useAppStore();
  const [boardsOpen, setBoardsOpen] = useState(false);

  // Same query key as the desktop board picker — shared cache.
  const { data: boardsData } = useQuery<Board[]>({ queryKey: ["boards"], queryFn: api.boards.list });
  const boards = boardsData ?? [];
  const activeBoard = boards.find((b) => b.id === activeBoardId) ?? boards[0] ?? null;
  const canSwitch = boards.length > 1;

  function logout() {
    clearToken();
    router.replace("/login");
  }

  const initial = (currentUser?.name?.[0] ?? "?").toUpperCase();

  return (
    <section
      aria-label={t("label")}
      data-testid="phone-account"
      data-region="phone-account"
      className="md:hidden shrink-0 px-3 pb-2"
    >
      {/* Who is signed in */}
      {currentUser && (
        <div className="flex items-center gap-4 min-h-14 px-1" data-testid="phone-account-user">
          <span
            aria-hidden
            className="shrink-0 w-10 h-10 rounded-full flex items-center justify-center text-base"
            style={{ background: C.bgSurface, color: C.textPrimary, fontWeight: 600 }}
          >
            {initial}
          </span>
          <span className="flex-1 min-w-0">
            <span className="block text-base truncate" style={{ color: C.textPrimary }}>
              {currentUser.name}
            </span>
            <span className="block text-sm truncate" style={{ color: C.textMuted }}>
              {currentUser.email}
            </span>
          </span>
        </div>
      )}

      {/* Board — the active one; tap to choose another */}
      {activeBoard && (
        <>
          {canSwitch ? (
            <button
              type="button"
              className={rowClass}
              aria-expanded={boardsOpen}
              aria-controls="phone-board-list"
              data-testid="phone-board-toggle"
              onClick={() => setBoardsOpen((o) => !o)}
            >
              <span className="flex-1 text-base" style={{ color: C.textPrimary }}>
                {t("board")}
              </span>
              <span className="flex items-center gap-2 min-w-0">
                <BoardMark board={activeBoard} />
                <span className="text-sm truncate" style={{ color: C.textSecondary }}>
                  {activeBoard.name}
                </span>
              </span>
              <ChevronDown
                size={18}
                aria-hidden
                className="shrink-0 transition-transform motion-reduce:transition-none"
                style={{ color: C.textMuted, transform: boardsOpen ? "rotate(180deg)" : undefined }}
              />
            </button>
          ) : (
            <div className="flex items-center gap-4 min-h-12 px-1" data-testid="phone-board-single">
              <span className="flex-1 text-base" style={{ color: C.textPrimary }}>
                {t("board")}
              </span>
              <span className="flex items-center gap-2 min-w-0">
                <BoardMark board={activeBoard} />
                <span className="text-sm truncate" style={{ color: C.textSecondary }}>
                  {activeBoard.name}
                </span>
              </span>
            </div>
          )}

          {canSwitch && boardsOpen && (
            <ul id="phone-board-list" role="radiogroup" aria-label={t("chooseBoard")} className="pb-1">
              {boards.map((board) => {
                const on = board.id === activeBoard.id;
                return (
                  <li key={board.id}>
                    <button
                      type="button"
                      role="radio"
                      aria-checked={on}
                      className={`${rowClass} pl-6`}
                      data-board={board.id}
                      onClick={() => {
                        setActiveBoardId(board.id);
                        setBoardsOpen(false);
                      }}
                    >
                      <BoardMark board={board} />
                      <span
                        className="flex-1 text-base truncate"
                        style={{ color: on ? C.textPrimary : C.textSecondary, fontWeight: on ? 600 : 400 }}
                      >
                        {board.name}
                      </span>
                      {on && <Check size={18} aria-hidden className="shrink-0" style={{ color: C.textPrimary }} />}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </>
      )}

      {/* Log out — a quiet text button (K11 "Neben"), never the loudest thing */}
      <button type="button" className={rowClass} data-testid="phone-logout" onClick={logout}>
        <LogOut size={20} aria-hidden className="shrink-0" style={{ color: C.textSecondary }} />
        <span className="flex-1 text-base" style={{ color: C.textSecondary }}>
          {tNav("logout")}
        </span>
      </button>
    </section>
  );
}

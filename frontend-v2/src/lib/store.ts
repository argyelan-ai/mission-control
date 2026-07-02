"use client";

import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { Board, BoardGroup } from "./types";

// ── Notification Store ─────────────────────────────────────────────────────────

export type NotificationType = "success" | "error" | "warning" | "info";

export interface AppNotification {
  id: string;
  type: NotificationType;
  message: string;
  timestamp: Date;
  persistent: boolean;
  dismissed: boolean;
}

interface NotificationState {
  notifications: AppNotification[];
  addNotification: (n: { type: NotificationType; message: string; persistent: boolean }) => void;
  dismissNotification: (id: string) => void;
  clearNotifications: () => void;
}

export const useNotificationStore = create<NotificationState>((set, get) => ({
  notifications: [],

  addNotification: ({ type, message, persistent }) => {
    const now = Date.now();
    // Deduplizierung: gleiche message+type innerhalb 3s ignorieren
    const recent = get().notifications.find(
      (n) => !n.dismissed && n.type === type && n.message === message && now - n.timestamp.getTime() < 3000
    );
    if (recent) return;

    const id = `notif-${now}-${Math.random().toString(36).slice(2, 7)}`;
    set((s) => ({
      notifications: [
        ...s.notifications,
        { id, type, message, timestamp: new Date(), persistent, dismissed: false },
      ],
    }));
  },

  dismissNotification: (id) =>
    set((s) => ({
      notifications: s.notifications.map((n) => (n.id === id ? { ...n, dismissed: true } : n)),
    })),

  clearNotifications: () =>
    set({ notifications: [] }),
}));

export interface AuthUser {
  id: string;
  email: string;
  name: string;
  role: string;
}

interface AppState {
  // Active board
  activeBoardId: string | null;
  setActiveBoardId: (id: string | null) => void;

  // Sidebar
  sidebarCollapsed: boolean;
  toggleSidebar: () => void;

  // Command palette
  commandPaletteOpen: boolean;
  setCommandPaletteOpen: (open: boolean) => void;

  // Board data (cached from API)
  boards: Board[];
  setBoards: (boards: Board[]) => void;

  boardGroups: BoardGroup[];
  setBoardGroups: (groups: BoardGroup[]) => void;

  // Auth
  currentUser: AuthUser | null;
  setCurrentUser: (user: AuthUser | null) => void;
}

export const useAppStore = create<AppState>()(
  persist(
    (set) => ({
      activeBoardId: null,
      setActiveBoardId: (id) => set({ activeBoardId: id }),

      sidebarCollapsed: false,
      toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),

      commandPaletteOpen: false,
      setCommandPaletteOpen: (open) => set({ commandPaletteOpen: open }),

      boards: [],
      setBoards: (boards) => set({ boards }),

      boardGroups: [],
      setBoardGroups: (boardGroups) => set({ boardGroups }),

      currentUser: null,
      setCurrentUser: (currentUser) => set({ currentUser }),
    }),
    {
      name: "mc-app-state",
      partialize: (state) => ({
        activeBoardId: state.activeBoardId,
        sidebarCollapsed: state.sidebarCollapsed,
      }),
    }
  )
);

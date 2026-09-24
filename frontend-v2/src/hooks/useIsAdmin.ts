import { useAppStore } from "@/lib/store";

/**
 * True when the logged-in user has the admin role.
 *
 * Terminals, the plugin shell and typing into an agent's live session are
 * admin-only on the backend (403 / WebSocket close 4003). The UI uses this
 * to show a short hint instead of an entry point that would only fail.
 * The backend stays the real gate — this only keeps the UI honest.
 */
export function useIsAdmin(): boolean {
  return useAppStore((s) => s.currentUser?.role === "admin");
}

import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import type { Board } from "./types";

/**
 * Whether a board switcher should be shown at all.
 *
 * Operator decision (12.10.2026): hide the board switcher while there is
 * only one board to switch to — the concept stays (boards still exist,
 * still creatable), it just has nothing to show until a second board
 * exists. Shared by the desktop sidebar (`BoardPicker`) and the phone
 * settings panel (`PhoneAccountPanel`) so the rule lives in exactly one
 * place instead of two copies drifting apart.
 */
export function canSwitchBoards(boards: Board[]): boolean {
  return boards.length > 1;
}

/**
 * Same decision, as a hook — for a caller that needs to know it BEFORE
 * deciding whether to render a board row at all (not just what goes inside
 * one). Shares the `["boards"]` query cache with `BoardPicker`/`BoardsCard`,
 * so this costs no extra request.
 *
 * Operator decision (12.10.2026, "V2"): with exactly one board the desktop
 * sidebar drops the board-name row entirely — not even plain text — so the
 * search/voice row becomes the visual top. `Sidebar` reads this to decide
 * whether to render that row's wrapper at all.
 */
export function useCanSwitchBoards(): boolean {
  const { data } = useQuery<Board[]>({ queryKey: ["boards"], queryFn: api.boards.list });
  return canSwitchBoards(data ?? []);
}

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

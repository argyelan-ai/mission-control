// What the result page offers for a record (pure, tested through the page):
// the ONE primary action (K11), the quiet secondary, and the ⋯ menu — all
// mapped from BenchTestDetail.allowed[] and the gate's fix hints, never
// guessed from the state alone.
import type { BenchTestDetail, ContestantResult, OverlayAction } from "../types";
import { isSafeEndpoint } from "./errors";

export type Primary = "harvest" | "kit" | "rerun" | null;

export function isLive(test: BenchTestDetail): boolean {
  return test.state === "waiting" || test.state === "running";
}

export function primaryOf(test: BenchTestDetail): Primary {
  if (isLive(test)) return null;
  const allowed = new Set(test.allowed);
  if (allowed.has("harvest") && test.gate.reasons.some((r) => r.fix === "harvest")) return "harvest";
  if (allowed.has("kit") && (test.gate.status === "green" || test.gate.status === "amber")) return "kit";
  if (allowed.has("rerun")) return "rerun";
  return null;
}

export type MenuId =
  | "kit"
  | "harvest"
  | "rerun_larger"
  | "recompose"
  | "rerender"
  | "archive"
  | "delete";

/** Allowed ids for the ⋯ menu, in display order, without the primary. */
export function menuIds(test: BenchTestDetail, primary: Primary): MenuId[] {
  if (isLive(test)) return [];
  const allowed = new Set(test.allowed);
  const out: MenuId[] = [];
  // A red gate still gets the kit as a PREVIEW (concept §6: "Red = preview
  // only, with the reason and a fix button") — the server refuses the
  // files (409), so nothing can be downloaded from it.
  const done = test.state === "done" || test.state === "done_missing";
  if (primary !== "kit" && (allowed.has("kit") || (done && test.gate.status === "red"))) out.push("kit");
  if (allowed.has("harvest") && primary !== "harvest") out.push("harvest");
  // The truncation block owns "rerun_larger" when there is one.
  if (allowed.has("rerun_larger") && !test.truncation) out.push("rerun_larger");
  for (const id of ["recompose", "rerender", "archive", "delete"] as const) if (allowed.has(id)) out.push(id);
  return out;
}

export interface OpenablePage {
  contestant: ContestantResult;
  entryId: string;
  run: number;
  /** A page kept when the time limit stopped the run — never a finished one. */
  incomplete: boolean;
}

/** The finished video run of each contestant — its page can be opened.
 *  A contestant without a finished page offers the incomplete page its
 *  stopped run left behind (time limit), marked as such; a stopped test
 *  offers only those. */
export function openablePages(test: BenchTestDetail): OpenablePage[] {
  if (isLive(test)) return [];
  const aborted = test.state === "aborted";
  const out: OpenablePage[] = [];
  for (const block of test.blocks)
    for (const c of block.contestants) {
      const run = aborted
        ? undefined
        : (c.runs.find((r) => r.is_video_run && r.state === "done") ?? c.runs.find((r) => r.state === "done"));
      if (run) {
        out.push({ contestant: c, entryId: run.entry_id, run: run.repeat_idx, incomplete: false });
        continue;
      }
      const kept = c.runs.find((r) => r.state === "failed" && r.incomplete_page);
      if (kept) out.push({ contestant: c, entryId: kept.entry_id, run: kept.repeat_idx, incomplete: true });
    }
  return out;
}

export function safeOverlayActions(test: BenchTestDetail): OverlayAction[] {
  if (isLive(test)) return [];
  return test.overlay_actions.filter((a) => isSafeEndpoint(a.endpoint));
}

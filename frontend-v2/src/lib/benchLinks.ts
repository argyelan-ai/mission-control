/**
 * Bench entry points for CORE pages (bench W1 contract, 24.10.2026).
 *
 * Core (Runtimes, QuickSheet, the recipe switch dialog, Inbox) may not import
 * from src/verticals/ (ADR-044 §4). It reaches the bench only through this
 * file: plain hrefs, the two vertical endpoints it needs, and a gate on
 * VERTICALS.benchStudio — a stripped build renders no bench button and
 * sends no bench request.
 *
 * The URL shape is the contract between the entry points (package D), the
 * test sheet (package B, which reads it back with parseBenchTestParams) and
 * the result page (package C). Change only with the lead (CONTRACT.md).
 */
import { request } from "./api";
import { VERTICALS } from "./verticals";

export const benchEnabled: boolean = VERTICALS.benchStudio;

export type BenchTrigger = "runtimes" | "quick" | "template" | "rerun" | "auto_switch" | "list";
export type BenchPresetId = "standard" | "quick" | "effort_ladder" | "rerun";

export interface BenchTestParams {
  runtimeId: string | null;
  preset: BenchPresetId | null;
  templateId: string | null;
  rerunOf: string | null;
  trigger: BenchTrigger;
}

const PRESETS: BenchPresetId[] = ["standard", "quick", "effort_ladder", "rerun"];
const TRIGGERS: BenchTrigger[] = ["runtimes", "quick", "template", "rerun", "auto_switch", "list"];

/** `/bench?test=1&…` — the list with the test sheet open (P3). */
export function benchTestHref(p: Partial<BenchTestParams> = {}): string {
  const q = new URLSearchParams({ test: "1" });
  if (p.runtimeId) q.set("runtime", p.runtimeId);
  if (p.preset) q.set("preset", p.preset);
  if (p.templateId) q.set("template", p.templateId);
  if (p.rerunOf) q.set("rerun", p.rerunOf);
  q.set("from", p.trigger ?? "list");
  return `/bench?${q.toString()}`;
}

/** The inverse of benchTestHref — null when the sheet is not requested. */
export function parseBenchTestParams(
  sp: Pick<URLSearchParams, "get"> | null | undefined,
): BenchTestParams | null {
  if (!sp || sp.get("test") !== "1") return null;
  const preset = sp.get("preset");
  const from = sp.get("from");
  return {
    runtimeId: sp.get("runtime") || null,
    preset: PRESETS.includes(preset as BenchPresetId) ? (preset as BenchPresetId) : null,
    templateId: sp.get("template") || null,
    rerunOf: sp.get("rerun") || null,
    trigger: TRIGGERS.includes(from as BenchTrigger) ? (from as BenchTrigger) : "list",
  };
}

/** Result page deep link (P5) — survives reload, shareable to oneself. */
export function benchResultHref(testId: string): string {
  return `/bench/r/${encodeURIComponent(testId)}`;
}

/** The bench list (P7). */
export const BENCH_LIST_HREF = "/bench";

/** Minimal core view of GET /api/v1/bench/runtime-summary — structurally a
 *  subset of the vertical's RuntimeTestSummary (checked by a vertical test). */
export interface BenchRuntimeSummary {
  runtime_id: string;
  testable: boolean;
  reason: { code: string; params: Record<string, unknown> } | null;
  primary: boolean;
  last_test: { id: string; title: string; created_at: string } | null;
  running_test_id: string | null;
  /** An auto test after a recipe switch that waits for the model (no runs yet). */
  planned_test?: { id: string; not_before: string | null } | null;
}

/** Runtimes page (P1): which cards get "Testen", which one is primary.
 *  Empty list when the bench is stripped. */
export async function fetchBenchRuntimeSummary(): Promise<BenchRuntimeSummary[]> {
  if (!benchEnabled) return [];
  return request<BenchRuntimeSummary[]>("/api/v1/bench/runtime-summary");
}

/** Recipe switch dialog (P1b): "Danach automatisch testen" after a
 *  successful switch. Returns the waiting test's id, or null when the bench
 *  is stripped. A failure here never undoes the switch — the caller shows
 *  a quiet hint. */
export async function scheduleBenchAutoTest(
  hostId: string,
  recipeSlug: string,
): Promise<{ test_id: string; not_before: string | null } | null> {
  if (!benchEnabled) return null;
  return request<{ test_id: string; not_before: string | null }>("/api/v1/bench/auto-tests", {
    method: "POST",
    body: JSON.stringify({ host_id: hostId, recipe_slug: recipeSlug }),
  });
}

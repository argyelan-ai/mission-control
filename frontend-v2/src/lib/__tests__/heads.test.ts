/**
 * Head launcher frontend model (docs/specs/head-launcher.md §7, §8, build
 * plan B1–B3): i18n parity, error codes → i18n keys, picker order and the
 * "always local" pre-selection.
 */
import { describe, expect, it, vi } from "vitest";
import en from "../../../messages/en.json";
import de from "../../../messages/de.json";
import {
  chooseInitialPair,
  defaultRestartMode,
  failReasonKey,
  headContextLine,
  headErrorKey,
  headListLine,
  headListTitle,
  HEAD_STEP_KEYS,
  headStepKey,
  lastHeadActivity,
  midLineStateWord,
  modelFamily,
  pairKey,
  pairLabel,
  pairReasonKey,
  pairShort,
  parseHeadOnBox,
  parseStep,
  prNumberFromUrl,
  newerHeadRunIdFor,
  runDurationSeconds,
  sortHeadsForList,
  splitPairs,
  supersededNeedsYouIds,
  type HeadPairsResponse,
} from "../heads";
import type { ChatEvent } from "../chatTypes";
import { mkPair, mkRun } from "./headFixtures";

function flatKeys(obj: unknown, prefix = ""): string[] {
  if (typeof obj !== "object" || obj === null) return [prefix];
  return Object.entries(obj as Record<string, unknown>).flatMap(([k, v]) => flatKeys(v, prefix ? `${prefix}.${k}` : k));
}

function resolve(tree: unknown, path: string): unknown {
  return path.split(".").reduce<unknown>((cur, p) => (cur && typeof cur === "object" ? (cur as Record<string, unknown>)[p] : undefined), tree);
}

const apiErr = (status: number, detail: unknown) => new Error(`API ${status}: ${JSON.stringify({ detail })}`);

describe("heads i18n (B1)", () => {
  it("en and de have exactly the same keys in the heads namespace", () => {
    expect(flatKeys((de as Record<string, unknown>).heads).sort()).toEqual(
      flatKeys((en as Record<string, unknown>).heads).sort(),
    );
  });

  it("the create-modal footer keys exist in both languages", () => {
    for (const k of ["cancel", "create", "retryUploads", "shortcutHint", "close", "title"]) {
      expect(typeof resolve(en, `tasks.createModal.${k}`)).toBe("string");
      expect(typeof resolve(de, `tasks.createModal.${k}`)).toBe("string");
    }
  });

  it("every reason, error and fail-reason key the code can return exists", () => {
    const codes = [
      "protocol_mismatch", "harness_not_supported", "needs_operator_decision", "unproven",
      "engine_not_ready", "box_busy", "quota_limit", "nonsense", null,
    ];
    for (const c of codes) expect(typeof resolve(en, `heads.${pairReasonKey(c)}`)).toBe("string");
    for (const r of ["stopped", "time_limit", "no_pr", "exit_3", "weird", null, "gh_identity_missing"]) {
      expect(typeof resolve(en, `heads.${failReasonKey(r).key}`)).toBe("string");
    }
  });
});

describe("hook bypass", () => {
  it("hook_bypassed has its own sentence in EN and DE", () => {
    const { key } = failReasonKey("hook_bypassed");
    expect(key).toBe("failReason.hook_bypassed");
    expect(resolve(en, `heads.${key}`)).toContain("--no-verify");
    expect(resolve(de, `heads.${key}`)).toContain("--no-verify");
  });
});

describe("local network blocked", () => {
  it("local_network_blocked names the operator step in EN and DE", () => {
    const { key } = failReasonKey("local_network_blocked");
    expect(key).toBe("failReason.local_network_blocked");
    expect(resolve(en, `heads.${key}`)).toContain("Local Network");
    expect(resolve(de, `heads.${key}`)).toContain("Lokales Netzwerk");
  });
});

describe("watchdog stop reasons", () => {
  it("no_progress and hard_limit have their own sentence in EN and DE", () => {
    for (const r of ["no_progress", "hard_limit", "time_limit"]) {
      const { key } = failReasonKey(r);
      expect(key).toBe(`failReason.${r}`);
      expect(typeof resolve(en, `heads.${key}`)).toBe("string");
      expect(typeof resolve(de, `heads.${key}`)).toBe("string");
    }
    expect(resolve(de, "heads.failReason.no_progress")).not.toBe(resolve(en, "heads.failReason.no_progress"));
  });

  it("a watchdog stop keeps the branch — restart continues by default", () => {
    for (const reason of ["no_progress", "hard_limit"]) {
      expect(defaultRestartMode({ reason, started_at: "2026-09-23T10:00:00Z" })).toBe("continue");
    }
  });
});

describe("headErrorKey (B2)", () => {
  it.each([
    ["pair_blocked", 422],
    ["engine_not_ready", 409],
    ["box_busy", 409],
    ["head_active", 409],
    ["repo_required", 422],
    ["spool_unavailable", 503],
    ["heads_disabled", 404],
  ])("maps %s to its own i18n key", (code, status) => {
    const key = headErrorKey(apiErr(status, { code }));
    expect(key).toBe(`errors.${code}`);
    expect(typeof resolve(en, `heads.${key}`)).toBe("string");
    expect(typeof resolve(de, `heads.${key}`)).toBe("string");
  });

  it("falls back to errors.unknown for plain text, unknown codes and non-errors", () => {
    expect(headErrorKey(new Error("API 500: Internal Server Error"))).toBe("errors.unknown");
    expect(headErrorKey(apiErr(400, { code: "made_up" }))).toBe("errors.unknown");
    expect(headErrorKey(apiErr(400, "string detail"))).toBe("errors.unknown");
    expect(headErrorKey("nope")).toBe("errors.unknown");
  });

  it("parseHeadOnBox reads the 409 head_on_box detail and nothing else", () => {
    expect(parseHeadOnBox(apiErr(409, { code: "head_on_box", run_id: "r1", task_id: "t1", title: "Fix it" }))).toEqual({
      run_id: "r1", task_id: "t1", title: "Fix it",
    });
    expect(parseHeadOnBox(apiErr(409, { code: "agent_busy", agents: [] }))).toBeNull();
    expect(parseHeadOnBox(apiErr(422, { code: "head_on_box" }))).toBeNull();
  });
});

describe("splitPairs (B3)", () => {
  const ompLocal = mkPair();
  const claudeLocal = mkPair({ harness: "claude", harness_label: "Claude Code", status: "experimental" });
  const ompQwenDown = mkPair({ runtime_slug: "qwen", runtime_label: "Qwen local", status: "blocked", reason_code: "engine_not_ready", live: false });
  const ompClaude = mkPair({ runtime_slug: "claude-sub", runtime_label: "Claude", locality: "cloud", status: "blocked", reason_code: "needs_operator_decision" });

  it("shows startable pairs first — ok before experimental — and the rest behind Show more", () => {
    const { primary, more } = splitPairs([ompClaude, claudeLocal, ompQwenDown, ompLocal]);
    expect(primary.map(pairKey)).toEqual([pairKey(ompLocal), pairKey(claudeLocal)]);
    expect(more.map(pairKey)).toEqual([pairKey(ompClaude), pairKey(ompQwenDown)]);
  });

  it("keeps a selected blocked pair visible on top (default whose engine is down)", () => {
    const { primary, more } = splitPairs([ompLocal, ompQwenDown], pairKey(ompQwenDown));
    expect(primary[0]).toBe(ompQwenDown);
    expect(more).toHaveLength(0);
  });
});

describe("chooseInitialPair (B3)", () => {
  const local = mkPair();
  const localDown = mkPair({ status: "blocked", reason_code: "engine_not_ready", live: false, startable: false });
  const cloudOk = mkPair({ runtime_slug: "cloud-x", locality: "cloud", status: "ok", startable: true });

  it("pre-selects the backend's default local pair", () => {
    const resp: HeadPairsResponse = { pairs: [cloudOk, local], default_pair: local };
    expect(chooseInitialPair(resp, null)).toBe(local);
  });

  it("keeps the default local even when its engine is down — never falls back to cloud", () => {
    const resp: HeadPairsResponse = { pairs: [cloudOk, localDown], default_pair: localDown };
    const chosen = chooseInitialPair(resp, null);
    expect(chosen?.locality).toBe("local");
    expect(chosen?.startable).toBe(false);
  });

  it("never pre-selects a cloud pair, not even a remembered startable one", () => {
    const resp: HeadPairsResponse = { pairs: [cloudOk, local], default_pair: local };
    expect(chooseInitialPair(resp, pairKey(cloudOk))).toBe(local);
  });

  it("uses a remembered local pair only while it is still startable", () => {
    const claudeLocal = mkPair({ harness: "claude", status: "experimental" });
    const resp: HeadPairsResponse = { pairs: [local, claudeLocal], default_pair: local };
    expect(chooseInitialPair(resp, pairKey(claudeLocal))).toBe(claudeLocal);
    const busy = { ...claudeLocal, status: "blocked" as const, reason_code: "box_busy", startable: false };
    expect(chooseInitialPair({ pairs: [local, busy], default_pair: local }, pairKey(busy))).toBe(local);
  });

  it("returns null when there is no local pair at all", () => {
    expect(chooseInitialPair({ pairs: [cloudOk], default_pair: null }, null)).toBeNull();
  });
});

describe("small helpers", () => {
  it("runDurationSeconds counts from start to exit, or to now", () => {
    expect(runDurationSeconds({ started_at: "2026-09-23T10:00:00Z", created_at: null, exited_at: "2026-09-23T10:12:00Z" })).toBe(720);
    const now = Date.parse("2026-09-23T10:01:00Z");
    expect(runDurationSeconds({ started_at: null, created_at: "2026-09-23T10:00:00Z", exited_at: null }, now)).toBe(60);
    expect(runDurationSeconds({ started_at: null, created_at: null, exited_at: null })).toBeNull();
  });

  it("prNumberFromUrl", () => {
    expect(prNumberFromUrl("https://github.com/o/r/pull/712")).toBe(712);
    expect(prNumberFromUrl(null)).toBeNull();
  });
});


describe("restart mode (review: continue needs an existing branch)", () => {
  it("runs that ended before the worktree existed restart fresh", async () => {
    const { canContinueRun, defaultRestartMode } = await import("../heads");
    for (const reason of ["not_picked_up", "gh_identity_missing", "gh_identity_unsafe", "sandbox_required", "prepare_failed", "spec_invalid", "box_busy", "previous_run_still_active"]) {
      expect(canContinueRun({ reason, started_at: "2026-09-23T10:00:00Z" })).toBe(false);
      expect(defaultRestartMode({ reason, started_at: "2026-09-23T10:00:00Z" })).toBe("fresh");
    }
    expect(canContinueRun({ reason: "stopped", started_at: null })).toBe(false);
  });

  it("runs with work on the branch continue", async () => {
    const { defaultRestartMode } = await import("../heads");
    for (const reason of [null, "stopped", "time_limit", "no_pr", "exit_1", "process_vanished"]) {
      expect(defaultRestartMode({ reason, started_at: "2026-09-23T10:00:00Z" })).toBe("continue");
    }
  });
});

// ── Heads in Chats (heads-sichtbar PR 2) ────────────────────────────────────

describe("modelFamily", () => {
  it("strips every trailing variant/quant tag, one at a time", () => {
    expect(modelFamily("GLM-5.3-Flash-EXL3")).toBe("GLM-5.3");
    expect(modelFamily("Qwen3.8-27B-NVFP4")).toBe("Qwen3.8-27B");
  });

  it("leaves a model with no recognised suffix untouched", () => {
    expect(modelFamily("glm")).toBe("glm");
    expect(modelFamily(null)).toBe("—");
  });
});

describe("pairShort", () => {
  it("harness label × model family, joined with the operator's pair word", () => {
    expect(pairShort({ harness: "omp", model: "GLM-5.3-Flash-EXL3" })).toBe("omp × GLM-5.3");
    expect(pairShort({ harness: "claude", model: "claude-opus-4-7" })).toBe("Claude Code × claude-opus-4-7");
  });
});

describe("headListTitle", () => {
  it("strips a leading bracket tag", () => {
    expect(headListTitle({ title: "[fixture] scrubbed head run for the reader tests" })).toBe(
      "scrubbed head run for the reader tests",
    );
  });

  it("strips more than one leading tag", () => {
    expect(headListTitle({ title: "[night] [retry] Fix flaky retry test" })).toBe("Fix flaky retry test");
  });

  it("falls back to the raw (trimmed) title when there is nothing left, or nothing to strip", () => {
    expect(headListTitle({ title: "[fixture]" })).toBe("[fixture]");
    expect(headListTitle({ title: "Fix flaky retry test" })).toBe("Fix flaky retry test");
    expect(headListTitle({ title: null })).toBe("");
  });
});

describe("parseStep", () => {
  it("parses the exact line step.txt writes, with the leading word", () => {
    expect(parseStep("step 7/7 finish run record · waiting for: nothing")).toEqual({
      n: 7, total: 7, name: "finish run record", waitingFor: "nothing",
    });
  });

  it("parses it without the leading word too", () => {
    expect(parseStep("5/7 independent review · waiting for: reviewer")).toEqual({
      n: 5, total: 7, name: "independent review", waitingFor: "reviewer",
    });
  });

  // Review finding on PR #756: live-checked against every real step.txt on
  // disk (24 heads) — 3 did not parse (2 missing "/total", 1 missing the
  // "waiting for:" clause entirely). Both real forms below.
  it("defaults total to 7 when step.txt omits '/total' (real form, 2/24 on disk)", () => {
    expect(parseStep("step 7 finished · waiting for: nothing")).toEqual({
      n: 7, total: 7, name: "finished", waitingFor: "nothing",
    });
  });

  it("parses a trailing clause that isn't 'waiting for:' — waitingFor is null, not a mis-read (real form, 1/24 on disk)", () => {
    expect(parseStep("step 7/7 done · run record written")).toEqual({
      n: 7, total: 7, name: "done", waitingFor: null,
    });
  });

  it("returns null for anything that does not match — never a guess", () => {
    expect(parseStep(null)).toBeNull();
    expect(parseStep("")).toBeNull();
    expect(parseStep("thinking about it")).toBeNull();
    // Sabotage: a step missing the "waiting for:" half must not parse
    // partially (a half-filled {n,total} would be worse than nothing).
    expect(parseStep("4/7 sabotage probe")).toBeNull();
  });
});

describe("HEAD_STEP_KEYS / headStepKey", () => {
  it("has exactly 8 keys, 0-indexed to the head procedure's own steps", () => {
    expect(HEAD_STEP_KEYS).toHaveLength(8);
    expect(headStepKey(0)).toBe("steps.context");
    expect(headStepKey(7)).toBe("steps.runRecord");
  });

  it("is i18n-translated in both languages (parity test above already covers the keys)", async () => {
    const en = (await import("../../../messages/en.json")).default as Record<string, unknown>;
    const steps = (en.heads as Record<string, unknown>).steps as Record<string, unknown>;
    for (const key of HEAD_STEP_KEYS) expect(typeof steps[key]).toBe("string");
  });

  it("returns null out of range rather than an undefined key", () => {
    expect(headStepKey(8)).toBeNull();
    expect(headStepKey(-1)).toBeNull();
  });
});

describe("sortHeadsForList", () => {
  it("orders needs_you before running/starting before every ended state", () => {
    // Distinct task ids: these five runs stand in for five UNRELATED tasks
    // here, purely to exercise group ordering — on a shared task id,
    // `needsYou` would (correctly) be judged superseded by whichever of the
    // others has the latest `created_at`, which is not what this test is
    // about (see the `sortHeadsForList + supersededNeedsYouIds` describe
    // block below for that behaviour).
    const needsYou = mkRun({ run_id: "a", task_id: "task-a", state: "needs_you" });
    const running = mkRun({ run_id: "b", task_id: "task-b", state: "running" });
    const starting = mkRun({ run_id: "c", task_id: "task-c", state: "starting" });
    const passed = mkRun({ run_id: "d", task_id: "task-d", state: "passed", exited_at: "2026-09-23T10:00:00Z" });
    const failed = mkRun({ run_id: "e", task_id: "task-e", state: "failed", exited_at: "2026-09-23T09:00:00Z" });
    const order = sortHeadsForList([passed, running, failed, needsYou, starting]).map((r) => r.run_id);
    expect(order).toEqual(["a", "b", "c", "d", "e"]);
  });

  it("sorts ended runs newest-end-first, within the ended group only", () => {
    const older = mkRun({ run_id: "old", state: "passed", exited_at: "2026-09-20T10:00:00Z" });
    const newer = mkRun({ run_id: "new", state: "failed", exited_at: "2026-09-23T10:00:00Z" });
    expect(sortHeadsForList([older, newer]).map((r) => r.run_id)).toEqual(["new", "old"]);
  });

  it("falls back to created_at when an ended run has no exited_at", () => {
    const noExit = mkRun({ run_id: "stopped-early", state: "stopped", exited_at: null, created_at: "2026-09-22T00:00:00Z" });
    const withExit = mkRun({ run_id: "ran-a-while", state: "stopped", exited_at: "2026-09-21T00:00:00Z" });
    expect(sortHeadsForList([withExit, noExit]).map((r) => r.run_id)).toEqual(["stopped-early", "ran-a-while"]);
  });

  it("is stable within a group — does not reorder ties", () => {
    const r1 = mkRun({ run_id: "1", state: "running" });
    const r2 = mkRun({ run_id: "2", state: "starting" });
    expect(sortHeadsForList([r1, r2]).map((r) => r.run_id)).toEqual(["1", "2"]);
  });

  // Review finding on PR #756 round 4: an answered `needs_you` stayed
  // pinned at the top forever, its own `state` field never changing once
  // the head moved on — the list itself has to notice a later run exists.
  it("a needs_you run superseded by a later run on the same task sorts into the ended group, newest-end-first", () => {
    const needsYou = mkRun({ run_id: "a", task_id: "t1", state: "needs_you", exited_at: "2026-09-03T00:00:00Z" });
    const successor = mkRun({ run_id: "b", task_id: "t1", restarted_from: "a", state: "failed", exited_at: "2026-09-10T00:00:00Z" });
    const older = mkRun({ run_id: "c", task_id: "t2", state: "passed", exited_at: "2026-09-05T00:00:00Z" });
    const order = sortHeadsForList([needsYou, successor, older]).map((r) => r.run_id);
    // Ended group, newest-end-first: successor (09-10) → older (09-05) →
    // the superseded needs_you (09-03) — never pinned ahead of them.
    expect(order).toEqual(["b", "c", "a"]);
  });

  it("a needs_you run that IS the newest run for its task stays pinned, even alongside an older ended run on the same task", () => {
    const olderEnded = mkRun({ run_id: "x", task_id: "t1", state: "passed", created_at: "2026-09-01T00:00:00Z", exited_at: "2026-09-01T00:00:00Z" });
    const needsYou = mkRun({ run_id: "y", task_id: "t1", state: "needs_you", created_at: "2026-09-10T00:00:00Z", exited_at: "2026-09-10T00:00:00Z" });
    expect(sortHeadsForList([olderEnded, needsYou]).map((r) => r.run_id)).toEqual(["y", "x"]);
  });
});

describe("supersededNeedsYouIds / newerHeadRunIdFor", () => {
  it("flags a needs_you run with a later run on the same task, by created_at", () => {
    const old = mkRun({ run_id: "a", task_id: "t1", state: "needs_you", created_at: "2026-09-01T00:00:00Z" });
    const successor = mkRun({ run_id: "b", task_id: "t1", state: "running", created_at: "2026-09-02T00:00:00Z" });
    expect(supersededNeedsYouIds([old, successor]).has("a")).toBe(true);
    expect(supersededNeedsYouIds([old, successor]).has("b")).toBe(false); // not needs_you — never flagged
  });

  it("flags a needs_you run named as another run's restarted_from, even with an identical created_at", () => {
    const old = mkRun({ run_id: "a", task_id: "t1", state: "needs_you", created_at: "2026-09-01T00:00:00Z" });
    const successor = mkRun({ run_id: "b", task_id: "t1", restarted_from: "a", state: "failed", created_at: "2026-09-01T00:00:00Z" });
    expect(supersededNeedsYouIds([old, successor]).has("a")).toBe(true);
  });

  it("does not flag the newest needs_you run for its task", () => {
    const needsYou = mkRun({ run_id: "a", task_id: "t1", state: "needs_you", created_at: "2026-09-10T00:00:00Z" });
    const olderEnded = mkRun({ run_id: "b", task_id: "t1", state: "passed", created_at: "2026-09-01T00:00:00Z" });
    expect(supersededNeedsYouIds([needsYou, olderEnded]).has("a")).toBe(false);
  });

  it("never flags a needs_you run with no other run on its task", () => {
    const solo = mkRun({ run_id: "a", task_id: "t1", state: "needs_you" });
    expect(supersededNeedsYouIds([solo]).size).toBe(0);
  });

  it("newerHeadRunIdFor prefers the direct restarted_from link over a same-task guess", () => {
    const old = mkRun({ run_id: "a", task_id: "t1", state: "needs_you", created_at: "2026-09-01T00:00:00Z" });
    const unrelatedLater = mkRun({ run_id: "z", task_id: "t1", state: "passed", created_at: "2026-09-05T00:00:00Z" });
    const directSuccessor = mkRun({ run_id: "b", task_id: "t1", restarted_from: "a", state: "running", created_at: "2026-09-02T00:00:00Z" });
    expect(newerHeadRunIdFor(old, [old, unrelatedLater, directSuccessor])).toBe("b");
  });

  it("newerHeadRunIdFor falls back to the newest other run on the same task with no direct link", () => {
    const old = mkRun({ run_id: "a", task_id: "t1", state: "needs_you", created_at: "2026-09-01T00:00:00Z" });
    const sibling = mkRun({ run_id: "b", task_id: "t1", state: "passed", created_at: "2026-09-05T00:00:00Z" });
    expect(newerHeadRunIdFor(old, [old, sibling])).toBe("b");
  });

  it("newerHeadRunIdFor is null for an orphaned run with no task and no restarted_from link", () => {
    const solo = mkRun({ run_id: "a", task_id: null, state: "needs_you" });
    expect(newerHeadRunIdFor(solo, [solo])).toBeNull();
  });
});

describe("midLineStateWord (review finding on PR #756 round 3)", () => {
  it("lowercases only the first letter, to match the running row's own lowercase phrase", () => {
    expect(midLineStateWord(en.heads.state.needs_you)).toBe("needs you");
    expect(midLineStateWord(en.heads.state.passed)).toBe("passed");
    expect(midLineStateWord(de.heads.state.needs_you)).toBe("braucht dich");
    expect(midLineStateWord(de.heads.state.passed)).toBe("bestanden");
  });

  it("leaves the i18n catalog itself sentence case — other surfaces show the word standalone", () => {
    expect(en.heads.state.passed).toBe("Passed");
    expect(de.heads.state.passed).toBe("Bestanden");
  });

  it("is a no-op on an already-empty or already-lowercase word", () => {
    expect(midLineStateWord("")).toBe("");
    expect(midLineStateWord("already lowercase")).toBe("already lowercase");
  });
});

describe("pairLabel (heads-sichtbar PR 3 §4: one separator, '×', everywhere)", () => {
  it("joins harness and runtime with '×', not '·'", () => {
    expect(pairLabel({ harness: "omp", runtime_label: "GLM local" })).toBe("omp × GLM local");
  });

  it("falls back to the runtime slug, then em dash, same as before", () => {
    expect(pairLabel({ harness: "omp", runtime_slug: "glm-local" })).toBe("omp × glm-local");
    expect(pairLabel({ harness: "omp" })).toBe("omp × —");
  });
});

// ── headContextLine / headListLine (heads-sichtbar PR 3 §4) ─────────────────
//
// `t` here is a minimal stand-in for `useTranslations("heads")`: it resolves
// a dotted key against the REAL catalog (so a typo'd key surfaces as the
// literal key, same failure mode as `next-intl` itself) and does the same
// plain `{var}` interpolation the component-level tests' `next-intl` mock
// does (see `src/test-setup.ts`) — no ICU plurals are involved here.
function makeHeadsT(tree: typeof en) {
  return (key: string, values?: Record<string, string | number>): string => {
    const raw = resolve(tree.heads, key);
    let s = typeof raw === "string" ? raw : key;
    if (values) for (const [k, v] of Object.entries(values)) s = s.split(`{${k}}`).join(String(v));
    return s;
  };
}
const tEn = makeHeadsT(en);
const tDe = makeHeadsT(de);

const GLM = "GLM-5.3-Flash-EXL3"; // modelFamily() strips the variant suffixes down to "GLM-5.3"

describe("headContextLine — the one 'pair + state/time fact' line", () => {
  it("needs_you: pair + exactly one state word, no time fact", () => {
    const run = mkRun({ state: "needs_you", harness: "omp", model: GLM });
    expect(headContextLine(run, tEn, "en")).toBe("omp × GLM-5.3 · needs you");
    expect(headContextLine(run, tDe, "de")).toBe("omp × GLM-5.3 · braucht dich");
  });

  it("starting: pair + 'starting…'", () => {
    const run = mkRun({ state: "starting", model: GLM });
    expect(headContextLine(run, tEn, "en")).toBe("omp × GLM-5.3 · starting…");
  });

  it("running: pair + running-for duration (falls back to bare pair with no started_at)", () => {
    vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-09-23T10:12:00Z"));
    const run = mkRun({ state: "running", model: GLM, started_at: "2026-09-23T10:00:00Z" });
    expect(headContextLine(run, tEn, "en")).toBe("omp × GLM-5.3 · running for 12 min");
    const noStart = mkRun({ state: "running", model: GLM, started_at: null, created_at: null });
    expect(headContextLine(noStart, tEn, "en")).toBe("omp × GLM-5.3");
    vi.restoreAllMocks();
  });

  // `formatAgeRounded`'s own age computation defaults to `new Date()`, which
  // (unlike `runDurationSeconds`'s `Date.now()` default above) is NOT moved
  // by mocking `Date.now` — the same reason `HeadChatRow.test.tsx`'s own
  // "ended" case checks only for the word "ago", never an exact day count.
  // A relative `exited_at` keeps this test exact without fighting that.
  it("ended: withAge (default) appends '… ago'; withAge:false stops at the state word", () => {
    const exitedAt = new Date(Date.now() - 3 * 86_400_000).toISOString();
    const run = mkRun({ state: "passed", model: GLM, exited_at: exitedAt });
    expect(headContextLine(run, tEn, "en")).toBe("omp × GLM-5.3 · passed · 3 d ago");
    expect(headContextLine(run, tEn, "en", { withAge: false })).toBe("omp × GLM-5.3 · passed");
  });
});

describe("headListLine — 'Head · <pair> · <state/time>', no end-of-run age", () => {
  it("prefixes with the translated 'Head' word, in both locales", () => {
    const run = mkRun({ state: "running", model: GLM, started_at: null, created_at: null });
    expect(headListLine(run, tEn, "en")).toBe("Head · omp × GLM-5.3");
    expect(headListLine(run, tDe, "de")).toBe("Head · omp × GLM-5.3");
  });

  it("an ended run has no '… ago' clause, unlike headContextLine's own default", () => {
    const exitedAt = new Date(Date.now() - 3 * 86_400_000).toISOString();
    const run = mkRun({ state: "passed", model: GLM, exited_at: exitedAt });
    expect(headListLine(run, tEn, "en")).toBe("Head · omp × GLM-5.3 · passed");
  });
});

describe("lastHeadActivity — HeadStateCard's 'Last: …' line", () => {
  const tool = (uuid: string, ts: string, title: string): ChatEvent =>
    ({ kind: "tool", uuid, ts, name: "bash", title, detail: {}, toolUseId: null, result: null, status: "done", stats: null, sidechain: false }) as ChatEvent;
  const message = (uuid: string, ts: string, role: "user" | "assistant" | "teammate", text: string): ChatEvent =>
    ({ kind: "message", uuid, ts, role, text, model: null, sidechain: false }) as ChatEvent;
  const thinking = (uuid: string, ts: string): ChatEvent => ({ kind: "thinking", uuid, ts, text: "hmm", sidechain: false }) as ChatEvent;

  it("picks the NEWEST tool title, scanning backward — not the first event in the window", () => {
    const events = [tool("t1", "2026-09-23T10:00:00Z", "older tool"), tool("t2", "2026-09-23T10:00:30Z", "newer tool")];
    expect(lastHeadActivity(events)).toEqual({ text: "newer tool", ts: "2026-09-23T10:00:30Z", kind: "tool" });
  });

  it("falls back to the newest assistant message's first line when the newest event has no title", () => {
    const events = [
      tool("t1", "2026-09-23T10:00:00Z", "earlier tool"),
      message("m1", "2026-09-23T10:00:10Z", "assistant", "Looking at the diff\nmore detail below"),
    ];
    expect(lastHeadActivity(events)).toEqual({ text: "Looking at the diff", ts: "2026-09-23T10:00:10Z", kind: "message" });
  });

  it("skips thinking/usage frames and the operator's own user turn — neither carries a sentence to show", () => {
    const events = [
      tool("t1", "2026-09-23T10:00:00Z", "the real last thing it did"),
      thinking("th1", "2026-09-23T10:00:05Z"),
      message("u1", "2026-09-23T10:00:10Z", "user", "do the thing"),
    ];
    expect(lastHeadActivity(events)).toEqual({ text: "the real last thing it did", ts: "2026-09-23T10:00:00Z", kind: "tool" });
  });

  it("is null for an empty window or one with nothing renderable", () => {
    expect(lastHeadActivity([])).toBeNull();
    expect(lastHeadActivity([thinking("th1", "2026-09-23T10:00:00Z")])).toBeNull();
  });

  // Review fix round 5: a raw assistant line went straight to the UI,
  // asterisks and all, and a bare code-fence opener counted as "the line".
  it("strips bold/inline-code marks from an assistant sentence (real omp run shape)", () => {
    const events = [message("m1", "2026-09-23T10:00:10Z", "assistant", "Run complete — **Status: passed**.")];
    expect(lastHeadActivity(events)).toEqual({ text: "Run complete — Status: passed.", ts: "2026-09-23T10:00:10Z", kind: "message" });
  });

  it("strips a leading heading/bullet mark and collapses `code` spans", () => {
    expect(lastHeadActivity([message("m1", "t1", "assistant", "## Next: run `pytest -q` again")]))
      .toEqual({ text: "Next: run pytest -q again", ts: "t1", kind: "message" });
    expect(lastHeadActivity([message("m2", "t2", "assistant", "- wait for reviewer result")]))
      .toEqual({ text: "wait for reviewer result", ts: "t2", kind: "message" });
  });

  it("a bare code-fence line is skipped — the next real line in the same message wins", () => {
    const events = [message("m1", "2026-09-23T10:00:10Z", "assistant", "```bash\npytest -q\n```")];
    expect(lastHeadActivity(events)).toEqual({ text: "pytest -q", ts: "2026-09-23T10:00:10Z", kind: "message" });
  });

  it("a message that is ONLY fence lines (nothing else) falls through, same as an all-skipped window", () => {
    expect(lastHeadActivity([message("m1", "t1", "assistant", "```\n```")])).toBeNull();
  });

  it("a tool event's own kind is \"tool\", an assistant sentence's is \"message\" — the card picks its icon from this", () => {
    expect(lastHeadActivity([tool("t1", "t", "bash: pytest")])?.kind).toBe("tool");
    expect(lastHeadActivity([message("m1", "t", "assistant", "Looking at the diff")])?.kind).toBe("message");
  });
});

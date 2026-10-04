/**
 * Head launcher frontend model (docs/specs/head-launcher.md §7, §8, build
 * plan B1–B3): i18n parity, error codes → i18n keys, picker order and the
 * "always local" pre-selection.
 */
import { describe, expect, it } from "vitest";
import en from "../../../messages/en.json";
import de from "../../../messages/de.json";
import {
  chooseInitialPair,
  defaultRestartMode,
  failReasonKey,
  headErrorKey,
  headListTitle,
  HEAD_STEP_KEYS,
  headStepKey,
  midLineStateWord,
  modelFamily,
  pairKey,
  pairReasonKey,
  pairShort,
  parseHeadOnBox,
  parseStep,
  prNumberFromUrl,
  runDurationSeconds,
  sortHeadsForList,
  splitPairs,
  type HeadPairsResponse,
} from "../heads";
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
    const needsYou = mkRun({ run_id: "a", state: "needs_you" });
    const running = mkRun({ run_id: "b", state: "running" });
    const starting = mkRun({ run_id: "c", state: "starting" });
    const passed = mkRun({ run_id: "d", state: "passed", exited_at: "2026-09-23T10:00:00Z" });
    const failed = mkRun({ run_id: "e", state: "failed", exited_at: "2026-09-23T09:00:00Z" });
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

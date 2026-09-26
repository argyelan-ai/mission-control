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
  pairKey,
  pairReasonKey,
  parseHeadOnBox,
  prNumberFromUrl,
  runDurationSeconds,
  splitPairs,
  type HeadPairsResponse,
} from "../heads";
import { mkPair } from "./headFixtures";

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

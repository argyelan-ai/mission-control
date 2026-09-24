/**
 * Night shift — frontend rules (ROADMAP E2): which pairs may be marked for
 * tonight, the pre-selection (never cloud), error/reason keys, report counts,
 * i18n parity EN/DE.
 */
import { describe, expect, it } from "vitest";
import en from "../../../messages/en.json";
import de from "../../../messages/de.json";
import {
  chooseTonightPair,
  invalidConfigFields,
  isHHMM,
  isMarkable,
  nightErrorKey,
  nightReasonKey,
  pairsForTonight,
  reportCounts,
} from "../nightShift";
import { pairKey } from "../heads";
import { mkPair } from "./headFixtures";

function flatKeys(obj: unknown, prefix = ""): string[] {
  if (typeof obj !== "object" || obj === null) return [prefix];
  return Object.entries(obj as Record<string, unknown>).flatMap(([k, v]) => flatKeys(v, prefix ? `${prefix}.${k}` : k));
}

function resolve(tree: unknown, path: string): unknown {
  return path.split(".").reduce<unknown>((cur, p) => (cur && typeof cur === "object" ? (cur as Record<string, unknown>)[p] : undefined), tree);
}

const localDown = mkPair({ status: "blocked", reason_code: "engine_not_ready", live: false });
const localBusy = mkPair({ runtime_slug: "qwen", status: "blocked", reason_code: "box_busy" });
const localWrongProtocol = mkPair({ harness: "claude", status: "blocked", reason_code: "protocol_mismatch" });
const cloudOk = mkPair({ runtime_slug: "cloud-x", runtime_label: "Cloud X", locality: "cloud", status: "ok", box_keys: [] });
const cloudUnproven = mkPair({ runtime_slug: "cloud-y", locality: "cloud", status: "blocked", reason_code: "unproven" });

describe("night shift i18n", () => {
  it("en and de have exactly the same keys in the nightShift namespace", () => {
    expect(flatKeys((de as Record<string, unknown>).nightShift).sort()).toEqual(
      flatKeys((en as Record<string, unknown>).nightShift).sort(),
    );
  });

  it("every reason/error/state key the UI can ask for exists in both catalogs", () => {
    const keys = [
      ...["lane_busy", "cloud_share", "engine_not_ready", "not_reached", "whatever", null].map(nightReasonKey),
      ...["queued", "waiting", "skipped", "started"].map((s) => `state.${s}`),
      ...["passed", "failed", "needs_you", "blocked", "running"].map((c) => `category.${c}`),
      "errors.unknown",
    ];
    for (const k of keys) {
      expect(typeof resolve((en as Record<string, unknown>).nightShift, k), `en ${k}`).toBe("string");
      expect(typeof resolve((de as Record<string, unknown>).nightShift, k), `de ${k}`).toBe("string");
    }
    expect((en as { settings: { sections: Record<string, string> } }).settings.sections.nightShift).toBe("Night shift");
    expect((de as { settings: { sections: Record<string, string> } }).settings.sections.nightShift).toBe("Nachtschicht");
  });
});

describe("pairs for tonight", () => {
  it("a model that is down or a busy box today may still be marked; other blocks stay", () => {
    expect(isMarkable(localDown)).toBe(true);
    expect(isMarkable(localBusy)).toBe(true);
    expect(isMarkable(localWrongProtocol)).toBe(false);
    expect(isMarkable(cloudUnproven)).toBe(false);
    const mapped = pairsForTonight([localDown, localWrongProtocol]);
    expect(mapped.map((p) => p.startable)).toEqual([true, false]);
    expect(mapped[0].reason_code).toBe("engine_not_ready"); // still says why, just not a block
  });

  it("pre-selects the local default even when its model is down tonight-wise; never a cloud pair", () => {
    const resp = { pairs: [cloudOk, localDown], default_pair: localDown };
    const pick = chooseTonightPair(resp, null);
    expect(pick && pairKey(pick)).toBe(pairKey(localDown));
    expect(pick?.startable).toBe(true);
    // a remembered cloud pair is ignored
    expect(pairKey(chooseTonightPair(resp, pairKey(cloudOk))!)).toBe(pairKey(localDown));
    expect(chooseTonightPair({ pairs: [cloudOk], default_pair: null }, null)).toBeNull();
  });
});

describe("codes and counts", () => {
  const apiError = (status: number, detail: unknown) => new Error(`API ${status}: ${JSON.stringify({ detail })}`);

  it("maps error codes to i18n keys", () => {
    expect(nightErrorKey(apiError(409, { code: "night_started" }))).toBe("errors.night_started");
    expect(nightErrorKey(apiError(422, { code: "pair_blocked", reason_code: "unproven" }))).toBe("errors.pair_blocked");
    expect(nightErrorKey(apiError(500, { code: "weird" }))).toBe("errors.unknown");
    expect(nightErrorKey(new Error("network"))).toBe("errors.unknown");
  });

  it("reads the bad fields of an invalid_config", () => {
    expect(invalidConfigFields(apiError(422, { code: "invalid_config", fields: ["start", "timezone"] }))).toEqual(["start", "timezone"]);
    expect(invalidConfigFields(apiError(409, { code: "night_started" }))).toEqual([]);
  });

  it("counts a report by category", () => {
    const counts = reportCounts({
      entries: [
        { task_id: "a", title: "A", started: true, category: "passed" },
        { task_id: "b", title: "B", started: true, category: "needs_you" },
        { task_id: "c", title: "C", started: false, category: "blocked" },
        { task_id: "d", title: "D", started: false, category: "blocked" },
      ],
    });
    expect(counts).toEqual({ passed: 1, failed: 0, needs_you: 1, blocked: 2, running: 0 });
    expect(reportCounts(null).passed).toBe(0);
  });

  it("HH:MM like the backend", () => {
    expect(["00:00", "22:00", "23:59"].every(isHHMM)).toBe(true);
    expect(["24:00", "6:00", "22:60", ""].some(isHHMM)).toBe(false);
  });
});

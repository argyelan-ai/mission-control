// Bench W1 contract guard (24.10.2026): the TS mirror, the links and the
// shared words stay in step with the backend contract.
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import en from "../../../../../messages/en.json";
import de from "../../../../../messages/de.json";
import {
  benchResultHref,
  benchTestHref,
  parseBenchTestParams,
  type BenchRuntimeSummary,
} from "@/lib/benchLinks";
import { parseCostConfirmation } from "../api";
import * as fx from "../fixtures";
import type {
  AbortCode,
  BenchTestState,
  BlockerCode,
  Harness,
  Section,
  WaitCode,
} from "../types";

const BACKEND_FIXTURES = path.resolve(
  __dirname,
  "../../../../../../backend/app/verticals/bench_studio/w1_fixtures",
);

function camel(name: string): string {
  const [head, ...rest] = name.split("_");
  return head + rest.map((p) => p[0].toUpperCase() + p.slice(1)).join("") + "Fixture";
}

describe("W1 fixtures mirror the backend contract", () => {
  const files = readdirSync(BACKEND_FIXTURES).filter((f) => f.endsWith(".json"));

  it("has one TS fixture per backend JSON file", () => {
    const tsNames = Object.keys(fx).sort();
    expect(tsNames).toEqual(files.map((f) => camel(f.replace(/\.json$/, ""))).sort());
  });

  it.each(files)("%s equals its TS mirror (rebuild with w1_fixtures/build.py)", (file) => {
    const json = JSON.parse(readFileSync(path.join(BACKEND_FIXTURES, file), "utf8"));
    const name = camel(file.replace(/\.json$/, "")) as keyof typeof fx;
    expect(fx[name]).toEqual(json);
  });

  it("the core runtime-summary view is a subset of the vertical type", () => {
    // Compile-time: assigning the vertical fixture to the core type must type-check.
    const core: BenchRuntimeSummary[] = fx.runtimeSummaryFixture;
    expect(core.length).toBeGreaterThan(0);
  });
});

describe("bench links (lib/benchLinks) round-trip", () => {
  it("parses back exactly what benchTestHref wrote", () => {
    const href = benchTestHref({
      runtimeId: "rt-1",
      preset: "effort_ladder",
      templateId: "tpl-1",
      rerunOf: "t-0",
      trigger: "template",
    });
    expect(href.startsWith("/bench?")).toBe(true);
    const parsed = parseBenchTestParams(new URLSearchParams(href.split("?")[1]));
    expect(parsed).toEqual({
      runtimeId: "rt-1",
      preset: "effort_ladder",
      templateId: "tpl-1",
      rerunOf: "t-0",
      trigger: "template",
    });
  });

  it("no sheet without test=1, unknown preset/trigger fall back", () => {
    expect(parseBenchTestParams(new URLSearchParams("runtime=x"))).toBeNull();
    const p = parseBenchTestParams(new URLSearchParams("test=1&preset=bogus&from=evil"));
    expect(p).toEqual({
      runtimeId: null,
      preset: null,
      templateId: null,
      rerunOf: null,
      trigger: "list",
    });
  });

  it("result deep link", () => {
    expect(benchResultHref("abc")).toBe("/bench/r/abc");
  });
});

describe("cost confirmation 409", () => {
  it("reads the contract detail out of request()'s error", () => {
    const err = new Error(
      `API 409: ${JSON.stringify({ detail: fx.costConfirmationFixture })}`,
    );
    expect(parseCostConfirmation(err)).toEqual(fx.costConfirmationFixture);
    expect(parseCostConfirmation(new Error("API 409: {\"detail\":\"busy\"}"))).toBeNull();
    expect(parseCostConfirmation(new Error("API 500: x"))).toBeNull();
  });
});

describe("benchShared words cover every contract code (EN + DE)", () => {
  type Words = Record<string, Record<string, string>>;
  const langs = { en: en.benchShared as unknown as Words, de: de.benchShared as unknown as Words };
  const states: BenchTestState[] = ["waiting", "running", "done", "done_missing", "aborted"];
  const sections: Section[] = ["running", "needs_look", "done"];
  const waits: WaitCode[] = ["model_fresh", "box_busy", "model_loading", "after_class_a", "box_asleep"];
  const aborts: AbortCode[] = [
    "stopped_by_operator",
    "task_deleted",
    "superseded_by_switch",
    "switch_failed",
    "all_runs_failed",
    "backend_restart",
  ];
  const blockers: BlockerCode[] = [
    "box_busy",
    "model_fresh",
    "box_asleep",
    "engine_down",
    "agent_busy",
    "protocol_mismatch",
    "no_api_key",
    "no_model",
  ];
  const harnesses: Harness[] = ["api", "omp", "hermes", "grok-build", "claude-code"];

  it.each(Object.entries(langs))("%s", (_lang, w) => {
    for (const s of states) expect(w.state[s], `state.${s}`).toBeTruthy();
    for (const s of sections) expect(w.section[s], `section.${s}`).toBeTruthy();
    for (const s of waits) expect(w.wait[s], `wait.${s}`).toBeTruthy();
    for (const s of aborts) expect(w.abort[s], `abort.${s}`).toBeTruthy();
    for (const s of blockers) expect(w.blocker[s], `blocker.${s}`).toBeTruthy();
    for (const s of harnesses) expect(w.harness[s], `harness.${s}`).toBeTruthy();
  });

  it("EN and DE have the same keys in every W1 namespace", () => {
    const keys = (o: unknown, prefix = ""): string[] =>
      o && typeof o === "object"
        ? Object.entries(o as Record<string, unknown>).flatMap(([k, v]) =>
            keys(v, `${prefix}${k}.`),
          )
        : [prefix];
    for (const ns of ["benchShared", "benchSheet", "benchResult", "benchHome"] as const) {
      expect(keys((de as Record<string, unknown>)[ns]).sort(), ns).toEqual(
        keys((en as Record<string, unknown>)[ns]).sort(),
      );
    }
  });
});

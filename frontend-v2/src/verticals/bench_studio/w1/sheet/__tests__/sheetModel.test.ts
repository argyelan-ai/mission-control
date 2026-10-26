import { describe, expect, it } from "vitest";
import { contestantsFixture, planStandardFixture } from "../../fixtures";
import {
  buildPicks,
  canAddOpponent,
  estimateFor,
  keepPicks,
  moneyFor,
  selectionFromPlan,
  toggleEffort,
  toggleHarness,
  type SheetSelection,
} from "../sheetModel";

const local = contestantsFixture.local[0];
const [subscription, paid] = contestantsFixture.cloud;
const all = [local, subscription, paid];

function base(): SheetSelection {
  return selectionFromPlan(planStandardFixture)!;
}

describe("sheetModel", () => {
  it("an unknown price outranks the subscription, as on the server (D4)", () => {
    const unknown = { ...subscription, runtime_id: "u-1", money: { ...subscription.money, cost_kind: "unknown" as const } };
    const picks = [
      { runtime_id: subscription.runtime_id, harness: "api" as const, efforts: ["default" as const] },
      { runtime_id: unknown.runtime_id, harness: "api" as const, efforts: ["default" as const] },
    ];
    expect(moneyFor(picks, [subscription, unknown]).kind).toBe("unknown");
  });

  it("starts from the plan's own picks", () => {
    expect(buildPicks(base())).toEqual(planStandardFixture.picks);
  });

  it("an opponent fills the second slot; two harnesses leave no room", () => {
    const s = base();
    expect(canAddOpponent(s)).toBe(true);
    const mix = toggleHarness(s, "omp");
    expect(mix.harnesses).toEqual(["api", "omp"]);
    expect(canAddOpponent(mix)).toBe(false);
    const duel = { ...s, opponentId: subscription.runtime_id };
    // With an opponent a harness chip swaps instead of adding (max 2).
    expect(toggleHarness(duel, "omp").harnesses).toEqual(["omp"]);
    expect(buildPicks(toggleHarness(duel, "omp"))).toHaveLength(2);
  });

  it("never leaves the subject without a harness or the ladder without a level", () => {
    const s = base();
    expect(toggleHarness(s, "api").harnesses).toEqual(["api"]);
    expect(toggleEffort({ ...s, efforts: ["max"] }, "max", true).efforts).toEqual(["max"]);
    const three = { ...s, efforts: ["off", "default", "max"] as SheetSelection["efforts"] };
    expect(toggleEffort(three, "high", true).efforts).toHaveLength(3);
  });

  it("agent picks never send an effort (D5)", () => {
    const s = { ...toggleHarness(base(), "omp"), efforts: ["max"] as SheetSelection["efforts"] };
    expect(buildPicks(s)[1]).toEqual({ runtime_id: local.runtime_id, harness: "omp", efforts: ["default"] });
  });

  it("money: local stays local with a hypothetical price, never real dollars (D3)", () => {
    const m = moneyFor(buildPicks(base()), all);
    expect(m).toMatchObject({ kind: "local", usd: null, hypotheticalUsd: 0.04, needsConfirmation: false });
  });

  it("money: a subscription opponent wins the wording, still no real dollars", () => {
    const m = moneyFor(buildPicks({ ...base(), opponentId: subscription.runtime_id }), all);
    expect(m).toMatchObject({ kind: "subscription", usd: null, needsConfirmation: false, provider: "Ollama Cloud" });
    expect(m.hypotheticalUsd).toBeCloseTo(0.08);
  });

  it("money: a paid opponent needs the OK, over the cap a second one (D4)", () => {
    const m = moneyFor(buildPicks({ ...base(), opponentId: paid.runtime_id }), all);
    expect(m).toMatchObject({ kind: "api_price", usd: 1.35, needsConfirmation: true, secondConfirmation: true });
    const cheap = moneyFor(buildPicks({ ...base(), opponentId: paid.runtime_id }), [
      local,
      { ...paid, money: { ...paid.money, usd: 0.3, second_confirmation: false } },
    ]);
    expect(cheap).toMatchObject({ usd: 0.3, needsConfirmation: true, secondConfirmation: false });
  });

  it("estimate: the ladder multiplies class A, the agent run is its own range", () => {
    const ladder = { ...toggleHarness(base(), "omp"), efforts: ["off", "default", "max"] as SheetSelection["efforts"] };
    const e = estimateFor(buildPicks(ladder), all);
    expect(e.a).toMatchObject({ min_s: 540, max_s: 780 });
    expect(e.b).toMatchObject({ min_s: 240, max_s: 5520 });
  });

  describe("keepPicks — a new plan for the same model keeps the operator's picks", () => {
    const clonePlan = () => JSON.parse(JSON.stringify(planStandardFixture)) as typeof planStandardFixture;
    const ladderPlan = () => {
      const p = clonePlan();
      p.preset = JSON.parse(JSON.stringify(p.presets[2]));
      return p;
    };

    it("same shape: harness and effort stay", () => {
      const prev: SheetSelection = { ...base(), harnesses: ["api", "omp"], efforts: ["off"] };
      const plan = clonePlan();
      const next = keepPicks(prev, false, selectionFromPlan(plan)!, plan);
      expect(next.harnesses).toEqual(["api", "omp"]);
      expect(next.efforts).toEqual(["off"]);
    });

    it("a level the new plan no longer offers falls back to the plan's", () => {
      const prev: SheetSelection = { ...base(), efforts: ["off"] };
      const plan = clonePlan();
      plan.contestants[0].effort.options = plan.contestants[0].effort.options.filter((o) => o.level !== "off");
      expect(keepPicks(prev, false, selectionFromPlan(plan)!, plan).efforts).toEqual(["default"]);
    });

    it("into the ladder the plan's levels win; inside the ladder the picked levels stay", () => {
      const plan = ladderPlan();
      plan.picks[0].efforts = ["off", "default", "max"];
      const single: SheetSelection = { ...base(), efforts: ["off"] };
      expect(keepPicks(single, false, selectionFromPlan(plan)!, plan).efforts).toEqual(["off", "default", "max"]);
      const two: SheetSelection = { ...base(), efforts: ["off", "max"] };
      expect(keepPicks(two, true, selectionFromPlan(plan)!, plan).efforts).toEqual(["off", "max"]);
    });

    it("out of the ladder several levels reset, one level stays", () => {
      const plan = clonePlan();
      const many: SheetSelection = { ...base(), efforts: ["off", "max"] };
      expect(keepPicks(many, true, selectionFromPlan(plan)!, plan).efforts).toEqual(["default"]);
      const one: SheetSelection = { ...base(), efforts: ["max"] };
      expect(keepPicks(one, true, selectionFromPlan(plan)!, plan).efforts).toEqual(["max"]);
    });

    it("another model re-fills from the plan", () => {
      const prev: SheetSelection = { ...base(), subjectId: "someone-else", harnesses: ["omp"], efforts: ["off"] };
      const plan = clonePlan();
      expect(keepPicks(prev, false, selectionFromPlan(plan)!, plan)).toEqual(selectionFromPlan(plan));
    });
  });
});

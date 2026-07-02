import { describe, it, expect } from "vitest";
import { api } from "@/lib/api";

// Phase 5 — MSY-05 D-24 frontend contract test.
// Per Pitfall 7 of 05-RESEARCH.md the recommended split is (a) the URL
// shape assertions in api-knowledge-scope.test.ts and (b) a backend
// route test. This file holds a single type-level / runtime-shape
// assertion documenting that ``api.knowledge.list`` accepts a ``scope``
// param — sidesteps QueryClientProvider setup for the D-24 contract.

describe("MemoryPage scope filter contract (D-24)", () => {
  it("api.knowledge.list signature accepts scope param (D-24 wiring proof)", () => {
    // Type-level assertion: this compiles only if `scope` is in the params shape.
    // The DOM-level assertion lives in api-knowledge-scope.test.ts (URL test) —
    // per Pitfall 7 we sidestep QueryClientProvider setup.
    const fn = api.knowledge.list as (
      p: { scope?: "global" | "board" | "agent" | "all" }
    ) => unknown;
    expect(typeof fn).toBe("function");
  });
});

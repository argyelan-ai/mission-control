import { describe, expect, it } from "vitest";

import { isSessionOnlyEffort } from "./chatTypes";

describe("isSessionOnlyEffort", () => {
  it("treats Claude's max/ultracode as session-only", () => {
    expect(isSessionOnlyEffort("max")).toBe(true);
    expect(isSessionOnlyEffort("ultracode")).toBe(true);
    expect(isSessionOnlyEffort("high")).toBe(false);
  });

  it("treats omp's off as session-only (the CLI never persists it)", () => {
    expect(isSessionOnlyEffort("off")).toBe(true);
  });
});

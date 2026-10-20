import { describe, it, expect } from "vitest";
import { formatElapsed, estimateTokens } from "./thinkingFormat";

describe("thinkingFormat", () => {
  it("formats an elapsed time as m:ss, and h:mm:ss past an hour", () => {
    expect(formatElapsed(0)).toBe("0:00");
    expect(formatElapsed(105_000)).toBe("1:45");
    expect(formatElapsed(59_999)).toBe("0:59");
    expect(formatElapsed(3_723_000)).toBe("1:02:03");
    expect(formatElapsed(-5)).toBe("0:00");
  });

  it("estimates tokens from characters (≈4 per token), rounded so it does not pretend precision", () => {
    expect(estimateTokens(0)).toBe(0);
    expect(estimateTokens(37)).toBe(10);
    expect(estimateTokens(23_600)).toBe(5_900);
    expect(estimateTokens(23_789)).toBe(5_900);
  });
});

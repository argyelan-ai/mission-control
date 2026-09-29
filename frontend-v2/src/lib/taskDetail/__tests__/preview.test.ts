import { describe, it, expect } from "vitest";
import { plainPreview } from "../preview";

describe("plainPreview — header previews without markdown syntax", () => {
  it.each([
    ["## Goal\n\nFix the scroll.", "Goal Fix the scroll."],
    ["[Boss] Result\n\n- [ ] Task only\n- [x] **Reusable** asset", "[Boss] Result Task only Reusable asset"],
    ["1. first\n2. second", "first second"],
    ["see [PR #638](https://example.test/pr/638) and `npm test`", "see PR #638 and npm test"],
    ["> quoted _note_ here", "quoted note here"],
    ["```bash\nnpm ci\n```", "npm ci"],
    ["keep snake_case_names and 2 * 3", "keep snake_case_names and 2 * 3"],
  ])("%j → %j", (input, out) => {
    expect(plainPreview(input)).toBe(out);
  });
});

/**
 * Round-4 user test (t3 NICE-5): the agent page said "Zuletzt gesehen: vor
 * 28462m" for an agent last seen 20 days ago — while its header, from the
 * same timestamp, said "vor 20 Tagen". One wording for both.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { timeAgo } from "@/lib/utils";
import de from "../../../../messages/de.json";
import en from "../../../../messages/en.json";

describe("last seen on the agent page", () => {
  it("uses the same relative time as the header, not raw minutes", () => {
    const page = readFileSync(join(__dirname, "..", "[id]", "page.tsx"), "utf8");
    expect(page).not.toContain('t("minsAgo"');
    expect(page).toContain("timeAgo(agent.last_seen_at, locale)");
    expect((de.agents.detail as Record<string, unknown>).minsAgo).toBeUndefined();
    expect((en.agents.detail as Record<string, unknown>).minsAgo).toBeUndefined();
  });

  it("20 days read as days", () => {
    const twentyDays = new Date(Date.now() - 28_462 * 60_000).toISOString();
    expect(timeAgo(twentyDays, "de")).toBe("vor 20 Tagen");
    expect(timeAgo(twentyDays, "en")).toBe("20 days ago");
  });
});

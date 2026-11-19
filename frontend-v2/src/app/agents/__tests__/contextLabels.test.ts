/**
 * Round-3 user test (t3 NICE-1): the agent page said "Context 2 %" and the
 * chat ring of the same agent "5 %", with no word on why. They measure two
 * sessions (the agent's last reported work session vs. this chat) — each now
 * says which one it is, in both languages.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import de from "../../../../messages/de.json";
import en from "../../../../messages/en.json";

describe("context figures name their session", () => {
  it("the agent page labels its figure as last reported, with a hint about the chat", () => {
    expect(en.agents.detail.contextReportedLabel).toBe("Context · last reported");
    expect(de.agents.detail.contextReportedLabel).toBe("Kontext · zuletzt gemeldet");
    expect(en.agents.detail.contextReportedHint).toMatch(/chat/);
    expect(de.agents.detail.contextReportedHint).toMatch(/Chat/);
    const page = readFileSync(join(__dirname, "..", "[id]", "page.tsx"), "utf8");
    expect(page).toContain('t("detail.contextReportedLabel")');
    expect(page).toContain('t("detail.contextReportedHint")');
  });

  it("the chat ring says it is this chat and points at the agent page", () => {
    // Live 30.10.2026: "Kontext 29 %" read as the running task's figure.
    // Name and tooltip say it is the chat conversation's.
    expect(en.sessions.contextUsed).toMatch(/^Chat context: /);
    expect(de.sessions.contextUsed).toMatch(/^Chat-Kontext: /);
    expect(en.sessions.contextRingTooltip).toMatch(/^Chat context \(this conversation\): /);
    expect(de.sessions.contextRingTooltip).toMatch(/^Chat-Kontext \(dieses Gespräch\): /);
    expect(en.sessions.contextRingTooltip).toMatch(/running task/);
    expect(de.sessions.contextRingTooltip).toMatch(/laufende Aufgabe/);
    expect(en.sessions.contextRingTooltip).toMatch(/agent page/);
    expect(de.sessions.contextRingTooltip).toMatch(/Agent-Seite/);
  });
});

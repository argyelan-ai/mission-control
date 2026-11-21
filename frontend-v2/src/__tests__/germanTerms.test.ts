/**
 * German everyday words for jargon the live test flagged (pruefteam 21.11):
 * the German catalog must not fall back to the English term.
 */
import { describe, it, expect } from "vitest";
import de from "../../messages/de.json";

type Tree = { [k: string]: string | Tree };
const get = (path: string): string => {
  const v = path.split(".").reduce<string | Tree | undefined>((n, p) => (n as Tree | undefined)?.[p], de as unknown as Tree);
  if (typeof v !== "string") throw new Error(`missing ${path}`);
  return v;
};

describe("German UI terms", () => {
  it.each([
    ["sessions.sectionPanels", "Ansichten"],
    ["sessions.effortLevel", "Denktiefe"],
    ["runtimes.hosts.roleHead", "Hauptbox"],
    ["runtimes.hosts.roleWorker", "Zweitbox"],
    ["runtimes.stage.topologyDuo", "2 Boxen"],
    ["tasks.createModal.title", "Neue Aufgabe"],
    ["tasks.createModal.create", "Aufgabe anlegen"],
  ])("%s = %s", (key, want) => {
    expect(get(key)).toBe(want);
  });

  it("the head texts name no registry jargon, and 'Head' has a one-line explanation", () => {
    expect(get("heads.needsRepoProject")).not.toMatch(/Registry/);
    expect(get("heads.sectionHint")).toMatch(/Aufgabe/);
  });

  it("the context sheet says when the chat gets summarised, not the CLI jargon", () => {
    // No invented threshold: nothing in the repo sets one (critic 21.11.).
    expect(get("sessions.contextPanelNote")).toContain("Kurz bevor der Kontext voll ist, fasst der Agent den Chat automatisch zusammen.");
    expect(get("sessions.contextPanelNote")).not.toMatch(/\d+ ?%/);
    expect(get("sessions.contextPanelNote")).not.toMatch(/CLI|Komprimierung/);
  });

  it("the reachable count names what is reachable", () => {
    expect(get("comms.reachable")).toMatch(/Agenten erreichbar/);
  });

  it("the uptime reads 'läuft seit …' and knows days", () => {
    expect(get("runtimes.stage.cornerUptimeHours")).toMatch(/^läuft seit /);
    expect(get("runtimes.stage.cornerUptimeDays")).toMatch(/Tag/);
  });

  it("the effort control explains itself (tooltip)", () => {
    expect(get("sessions.effortLevelHint")).toMatch(/Denktiefe/);
  });

  it("no effort sentence keeps the English word", () => {
    const s = (de as unknown as { sessions: Record<string, unknown> }).sessions;
    const effort = Object.entries(s).filter(([k, v]) => k.startsWith("effort") && typeof v === "string");
    expect(effort.filter(([, v]) => /Effort/.test(v as string)).map(([k]) => k)).toEqual([]);
  });
});

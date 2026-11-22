/**
 * "New task" speaks one language: rendered with the REAL next-intl and the real
 * catalogs (the global test mock always resolves English), the German form
 * shows no English UI words and the English form shows no German ones.
 *
 * Covers both layouts: the two-pane CreateTaskModal (with "Advanced" open) and
 * the stacked TaskFormFields the schedule JobModal embeds (Structured mode).
 * Every visible text node plus placeholder / aria-label / title / <option>
 * text is collected — a screen reader or tooltip in the wrong language counts.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import de from "../../../../messages/de.json";
import en from "../../../../messages/en.json";
import type { Agent } from "@/lib/types";

vi.unmock("next-intl");
vi.setConfig({ testTimeout: 15_000 });

const { NextIntlClientProvider } = await import("next-intl");
const { CreateTaskModal } = await import("../CreateTaskModal");
const { TaskFormFields, EMPTY_TASK_FORM_PAYLOAD } = await import("../TaskFormFields");
const { api } = await import("@/lib/api");

const AGENT = {
  id: "a1", name: "Builder", role: null, status: "online", run_state: "idle",
  board_id: null, workspace_path: null, agent_runtime: null,
} as unknown as Agent;

function wrap(locale: "de" | "en", node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <NextIntlClientProvider locale={locale} messages={locale === "de" ? de : en} onError={() => {}}>
      <QueryClientProvider client={qc}>{node}</QueryClientProvider>
    </NextIntlClientProvider>,
  );
}

/** Every string a person can see or hear on the form. */
function collectStrings(root: HTMLElement): string[] {
  const out: string[] = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const s = n.textContent?.trim();
    if (s) out.push(s);
  }
  root.querySelectorAll("[placeholder],[aria-label],[title]").forEach((el) => {
    for (const a of ["placeholder", "aria-label", "title"]) {
      const v = el.getAttribute(a);
      if (v) out.push(v);
    }
  });
  return out;
}

// Words that only show up when an English label leaked into German.
const ENGLISH_IN_DE = [
  /\bTask\b/, /\bNew task\b/i, /\bCreate task\b/i, /\bApproval\b/, /\bSkip review\b/i, /\bHuman review\b/i,
  /\bDeadline\b/, /\bReport-Back\b/i, /\bReference files\b/i, /\bAdd files\b/i, /\bNo phase\b/i,
  /\bNo deliverable\b/i, /\bProject \(optional\)/, /\bBlocker to me\b/i, /\bE2E test\b/i, /\bOperator-Intake\b/,
  /\bScope-Out\b/i, /\bAcceptance Criteria\b/i, /\bCancel\b/, /\bClose\b/, /\bAdvanced\b/, /\bAssignment\b/,
  /\bExecution\b/, /\bPriority\b/, /\bTitle\b/, /\bDescription\b/, /\bTemplate\b/, /\bCredential\b/,
  /\bPlanner\b/, /\bPlan first\b/, /\bNo repository\b/i, /\bCreate new\b/i, /\bRemove URL\b/i, /\bAdd URL\b/i,
];
// Words / letters that only show up when a German label leaked into English.
const GERMAN_IN_EN = [
  /[äöüÄÖÜß]/, /\bAufgabe/, /\bTitel\b/, /\bBeschreibung\b/, /\bProjekt\b/, /\bZuweisung\b/, /\bVorlage\b/,
  /\bErweitert\b/, /\bAuftrag/, /\bPlanung\b/, /\bDirekt\b/, /\bSchnell\b/, /\bStrukturiert\b/, /\bArbeitsplatz\b/,
  /\bRisiken\b/, /\bKanal\b/, /\bErlaubt\b/, /\bNur\b/, /\bwählen\b/, /\bwaehlen\b/, /\bzuruecksetzen\b/,
  /\bAbbrechen\b/, /\bSchliessen\b/, /\bAnlegen\b/i, /\bBasiert\b/, /\bAutonomie\b/, /\bEinmalig\b/, /\bVault\b/,
];

// Plus: any catalog sentence of the OTHER language (taskForm, createModal,
// heads) that differs from this language's own — catches a label that was
// swapped for the wrong catalog or hard-coded back in one language.
type Tree = { [k: string]: string | Tree };
function values(t: Tree): Map<string, string> {
  const out = new Map<string, string>();
  const walk = (n: Tree, p: string) => {
    for (const [k, v] of Object.entries(n)) typeof v === "string" ? out.set(p + k, v) : walk(v, `${p}${k}.`);
  };
  walk(t, "");
  return out;
}
function foreignSentences(own: "de" | "en"): Set<string> {
  const pick = (c: unknown) => {
    const r = c as { taskForm: Tree; tasks: { createModal: Tree }; heads: Tree };
    return values({ taskForm: r.taskForm, createModal: r.tasks.createModal, heads: r.heads });
  };
  const mine = pick(own === "de" ? de : en);
  const other = pick(own === "de" ? en : de);
  const out = new Set<string>();
  // Only whole labels of 2+ words or 6+ letters: short shared words ("Agent",
  // "Login", "Format") are the same in both languages on purpose.
  for (const [k, v] of other) if (mine.get(k) !== v && (/\s/.test(v) || v.length >= 6) && !v.includes("{")) out.add(v);
  for (const v of mine.values()) out.delete(v);
  return out;
}

function leaks(strings: string[], patterns: RegExp[], locale: "de" | "en"): string[] {
  const foreign = foreignSentences(locale);
  return strings.filter((s) => patterns.some((p) => p.test(s)) || foreign.has(s));
}

async function openModal(locale: "de" | "en") {
  wrap(locale, <CreateTaskModal activeBoardId="board-1" agents={[AGENT]} />);
  const trigger = screen.getByRole("button", { name: locale === "de" ? "Neue Aufgabe" : "New task" });
  await userEvent.click(trigger);
  const dialog = await screen.findByRole("dialog");
  // Pick a template (header chip + template placeholders) and open "Advanced"
  // with every sub-panel so the whole form is on screen.
  // The template chip comes first; the task-type pill further down has the same word.
  await userEvent.click(screen.getAllByRole("button", { name: locale === "de" ? "Fehler beheben" : "Bug fix" })[0]);
  await userEvent.click(screen.getByRole("button", { name: locale === "de" ? /Erweitert/ : /Advanced/ }));
  await userEvent.click(await screen.findByRole("button", { name: "Login" }));
  await userEvent.click(screen.getByRole("button", { name: locale === "de" ? "Rückmeldung" : "Report back" }));
  await waitFor(() => expect(dialog.querySelectorAll("select").length).toBeGreaterThanOrEqual(4));
  return dialog;
}

function renderStacked(locale: "de" | "en") {
  const { container } = wrap(
    locale,
    <TaskFormFields
      value={{ ...EMPTY_TASK_FORM_PAYLOAD, plannerMode: "with_planner", requiresAuth: true, reportBack: true }}
      onChange={() => {}}
      activeBoardId="board-1"
      agents={[AGENT]}
      mode="strukturiert"
    />,
  );
  return container;
}

describe("New task form — one language per screen", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("{}", { status: 404, headers: { "Content-Type": "application/json" } }),
    );
    vi.spyOn(api.projects, "list").mockResolvedValue([]);
    vi.spyOn(api.credentials, "list").mockResolvedValue([]);
    vi.spyOn(api.repos, "list").mockResolvedValue([]);
  });

  it("German modal: no English UI words, 'Aufgabe' instead of 'Task'", async () => {
    const dialog = await openModal("de");
    const strings = collectStrings(dialog);
    expect(strings).toContain("Neue Aufgabe");
    expect(strings).toContain("Aufgabe anlegen");
    expect(leaks(strings, ENGLISH_IN_DE, "de")).toEqual([]);
  });

  it("English modal: no German words or umlauts", async () => {
    const dialog = await openModal("en");
    const strings = collectStrings(dialog);
    expect(strings).toContain("New task");
    expect(strings).toContain("Create task");
    expect(leaks(strings, GERMAN_IN_EN, "en")).toEqual([]);
  });

  it("German stacked form (schedule template): no English UI words", async () => {
    const root = renderStacked("de");
    await waitFor(() => expect(root.textContent).toContain("Strukturiert"));
    expect(leaks(collectStrings(root), ENGLISH_IN_DE, "de")).toEqual([]);
  });

  it("English stacked form (schedule template): no German words", async () => {
    const root = renderStacked("en");
    await waitFor(() => expect(root.textContent).toContain("Structured"));
    expect(leaks(collectStrings(root), GERMAN_IN_EN, "en")).toEqual([]);
  });

  it("taskForm has the same keys in German and English", () => {
    const leaves = (t: Tree, p = ""): string[] =>
      Object.entries(t).flatMap(([k, v]) => (typeof v === "string" ? [p + k] : leaves(v, `${p}${k}.`)));
    const d = (de as unknown as { taskForm: Tree }).taskForm;
    const e = (en as unknown as { taskForm: Tree }).taskForm;
    expect(leaves(d).sort()).toEqual(leaves(e).sort());
  });
});

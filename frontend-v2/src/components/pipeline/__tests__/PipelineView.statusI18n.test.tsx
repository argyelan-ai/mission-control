/**
 * Status words follow the UI language: German UI → German words, English UI →
 * English words. Renders with the REAL next-intl provider (the global test
 * mock only knows the English catalog) and checks the one status vocabulary
 * (lib/taskDetail/statusLabels.ts) against both catalogs.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { STATUS_LABEL_KEY } from "@/lib/taskDetail/statusLabels";
import type { PipelineTask, TaskStatus } from "@/lib/types";
import en from "../../../../messages/en.json";
import de from "../../../../messages/de.json";

vi.mock("next-intl", async () => vi.importActual("next-intl"));

const { NextIntlClientProvider } = await vi.importActual<typeof import("next-intl")>("next-intl");
const { default: PipelineView } = await import("../PipelineView");

const EXPECTED = {
  en: { inbox: "Inbox", in_progress: "In Progress", review: "Review", user_test: "User Test", waiting: "Waiting", done: "Done", blocked: "Blocked", failed: "Failed", aborted: "Aborted" },
  de: { inbox: "Neu", in_progress: "In Arbeit", review: "Zur Prüfung", user_test: "Nutzertest", waiting: "Wartet", done: "Erledigt", blocked: "Blockiert", failed: "Fehlgeschlagen", aborted: "Abgebrochen" },
} satisfies Record<"en" | "de", Record<TaskStatus, string>>;

const card = (id: string): PipelineTask => ({ id, title: `Task ${id}`, priority: "medium", parent_task_id: null, agent: null, has_blocked_deps: false });

type PipelineData = Awaited<ReturnType<typeof api.tasks.pipeline>>;
const FULL: PipelineData = {
  pipeline: {
    inbox: [card("a")], in_progress: [card("b")], review: [card("c")], user_test: [card("d")],
    waiting: [card("e")], blocked: [card("f")], failed: [card("g")], aborted: [card("h")],
  },
  done_count: 0,
  failed_count: 0,
} as PipelineData;

function renderPipeline(locale: "en" | "de", data: PipelineData = FULL) {
  vi.spyOn(api.tasks, "pipeline").mockResolvedValue(data);
  vi.spyOn(api.tasks, "list").mockResolvedValue([]);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <NextIntlClientProvider locale={locale} messages={locale === "de" ? de : en} timeZone="UTC">
      <QueryClientProvider client={qc}>
        <PipelineView boardId="board-1" agents={[]} />
      </QueryClientProvider>
    </NextIntlClientProvider>,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("status words follow the UI language", () => {
  it.each(["en", "de"] as const)("the catalog (%s) has every status word of the one vocabulary", (locale) => {
    const tasks = (locale === "de" ? de : en).tasks as unknown as Record<string, string>;
    for (const [status, key] of Object.entries(STATUS_LABEL_KEY)) {
      expect(tasks[key], `${locale} ${key}`).toBe(EXPECTED[locale][status as TaskStatus]);
    }
  });

  it("German UI → German lane headers", async () => {
    renderPipeline("de");
    for (const word of ["Neu", "In Arbeit", "Zur Prüfung", "Nutzertest", "Wartet", "Blockiert", "Fehlgeschlagen", "Abgebrochen"]) {
      expect(await screen.findByText(word)).toBeInTheDocument();
    }
    expect(screen.queryByText("Blocked")).toBeNull();
    expect(screen.queryByText("In Progress")).toBeNull();
  });

  it("English UI → English lane headers", async () => {
    renderPipeline("en");
    for (const word of ["Inbox", "In Progress", "Review", "User Test", "Waiting", "Blocked", "Failed", "Aborted"]) {
      expect(await screen.findByText(word)).toBeInTheDocument();
    }
    expect(screen.queryByText("Blockiert")).toBeNull();
  });
});

describe("pipeline header and empty state follow the UI language", () => {
  it.each([
    ["de", "3 fehlgeschlagen"],
    ["en", "3 failed"],
  ] as const)("failed count (%s)", async (locale, text) => {
    renderPipeline(locale, { ...FULL, failed_count: 3 });
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it.each([
    ["de", "Keine aktiven Aufgaben."],
    ["en", "No active tasks."],
  ] as const)("no visible lane (%s)", async (locale, text) => {
    // Active work only in a column the board does not show as a lane.
    const empty = { inbox: [], in_progress: [], review: [], user_test: [], waiting: [], blocked: [], failed: [], aborted: [] };
    renderPipeline(locale, { ...FULL, pipeline: { ...empty, planning: [card("x")] } as unknown as PipelineData["pipeline"] });
    expect(await screen.findByText(text)).toBeInTheDocument();
  });
});

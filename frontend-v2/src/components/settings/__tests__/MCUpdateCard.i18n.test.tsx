/**
 * The update card in German and English with the REAL next-intl and catalogs
 * (the global test mock always resolves English). Sabotage probe (c) of the
 * update plan: a German text missing → this test is red.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import de from "../../../../messages/de.json";
import en from "../../../../messages/en.json";
import { KNOWN_CHECKS, STEPS } from "@/lib/mcUpdate";

vi.unmock("next-intl");
vi.setConfig({ testTimeout: 15_000 });

const m = vi.hoisted(() => ({ status: vi.fn(), history: vi.fn(), stepUpAvailability: vi.fn() }));
vi.mock("@/lib/api", () => ({
  api: {
    system: { update: { status: m.status, history: m.history, preflight: vi.fn(), start: vi.fn(), rollback: vi.fn(), resume: vi.fn() } },
    auth: { stepUp: vi.fn(), stepUpAvailability: m.stepUpAvailability },
  },
}));
vi.mock("@/lib/notify", () => ({ notify: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() } }));

const { NextIntlClientProvider } = await import("next-intl");
const { MCUpdateCard } = await import("../MCUpdateCard");

type Tree = { [k: string]: string | Tree };
function leaves(tree: Tree, prefix = ""): string[] {
  return Object.entries(tree).flatMap(([k, v]) => (typeof v === "string" ? [prefix + k] : leaves(v, `${prefix}${k}.`)));
}
const enU = (en as unknown as { settings: { update: Tree } }).settings.update;
const deU = (de as unknown as { settings: { update: Tree } }).settings.update;

describe("settings.update catalog", () => {
  it("German and English have exactly the same keys", () => {
    expect(leaves(deU).sort()).toEqual(leaves(enU).sort());
  });

  it("every phase-bar step and every known check has words in both languages", () => {
    for (const cat of [enU, deU]) {
      const keys = new Set(leaves(cat));
      for (const s of STEPS) expect(keys.has(`steps.${s}`)).toBe(true);
      for (const c of KNOWN_CHECKS) expect([...keys].some((k) => k.startsWith(`checks.${c}.`))).toBe(true);
    }
  });

  it("no German text is a copy of the English one (except names and versions)", () => {
    const same = leaves(enU).filter((k) => {
      const get = (t: Tree) => k.split(".").reduce<string | Tree>((n, p) => (n as Tree)[p], t);
      return get(enU) === get(deU);
    });
    // Words that are the same in both languages.
    expect(same.sort()).toEqual(["details", "title"].sort());
  });
});

const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

function renderCard(locale: "de" | "en") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <NextIntlClientProvider locale={locale} messages={locale === "de" ? de : en} timeZone="Europe/Zurich">
      <QueryClientProvider client={qc}>
        <MCUpdateCard
          version={{ current: "0.9.2", latest: "v0.9.3", release_url: null, update_available: true }}
          timing={{ activeMs: 50, idleMs: 50, noAnswerMs: 50, reloadMs: 10_000, expectMs: 60_000 }}
          reload={vi.fn()}
        />
      </QueryClientProvider>
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  m.status.mockReset();
  m.history.mockResolvedValue({
    runs: [
      {
        run_id: "20261018T100000Z-000001", source: "ui", from_version: "v0.9.1", target_version: "v0.9.2",
        phase: "done", error: null, failure_kind: null, backup: null, started_by: "Admin",
        started_at: ago(86_400_000), finished_at: ago(86_000_000), rollback_requested_by: null,
        rollback_requested_at: null, drain_lifted_at: null,
      },
    ],
  });
  m.stepUpAvailability.mockResolvedValue({ available: true, reason: null, ttl_seconds: 300 });
});

describe("German screen", () => {
  it("available: German sentence and button, history in German", async () => {
    m.status.mockResolvedValue({
      drain: { active: false, run_id: null, expires_at: null },
      running_work: { tasks: 0, group_rounds: 0 },
      bridge_reachable: true, stale: false, run: null,
    });
    renderCard("de");
    await waitFor(() => expect(screen.getByTestId("update-sentence")).toHaveTextContent("Version 0.9.3 ist verfügbar · du hast 0.9.2"));
    expect(screen.getByRole("button", { name: "Update prüfen" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Verlauf \(1\)/ })).toBeInTheDocument();
  });

  it("set back: the step and the sentence are German", async () => {
    m.status.mockResolvedValue({
      drain: { active: false, run_id: null, expires_at: null },
      running_work: { tasks: 0, group_rounds: 0 },
      bridge_reachable: true, stale: false,
      run: {
        run_id: "20261019T115000Z-abc123", phase: "rolled_back", from: { tag: "v0.9.2" }, target: { tag: "v0.9.3" },
        running: false, finished_at: ago(60_000),
        events: [{ phase: "up", event: "intent" }, { phase: "rolling_back", event: "intent" }],
      },
    });
    renderCard("de");
    await waitFor(() =>
      expect(screen.getByTestId("update-sentence")).toHaveTextContent(
        "Update fehlgeschlagen bei „Neustart“. MC wurde automatisch auf 0.9.2 zurückgesetzt",
      ),
    );
    expect(screen.getByRole("button", { name: "Log kopieren" })).toBeInTheDocument();
  });
});

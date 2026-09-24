/**
 * Night shift UI (ROADMAP E2): Tonight list, the task-detail switch, the
 * settings section.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api } from "@/lib/api";
import { mkPair, mkRun } from "@/lib/__tests__/headFixtures";
import type { NightConfig, NightEntry, NightTonight } from "@/lib/nightShift";
import { TonightListView } from "../TonightList";
import { NightShiftToggle } from "../NightShiftToggle";
import { NightShiftTab } from "@/components/settings/NightShiftTab";

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const cfg: NightConfig = {
  enabled: true,
  start: "22:00",
  end: "06:00",
  timezone: "Europe/Berlin",
  cloud_share: 30,
  active: false,
  window: { night: "2026-09-24", starts_at: "2026-09-24T20:00:00+00:00", ends_at: "2026-09-25T04:00:00+00:00" },
};

function entry(over: Partial<NightEntry> = {}): NightEntry {
  return {
    task_id: "t-1",
    title: "Fix flaky retry test",
    task_status: "inbox",
    harness: "omp",
    runtime_slug: "glm-local",
    locality: "local",
    marked_at: "2026-09-24T10:00:00+00:00",
    night: null,
    state: "queued",
    reason: null,
    run: null,
    ...over,
  };
}

const ompLocal = mkPair();

beforeEach(() => {
  vi.restoreAllMocks();
  try { window.localStorage.removeItem("mc.heads.lastPair"); } catch { /* storage may be unavailable */ }
  vi.spyOn(api.heads, "pairs").mockResolvedValue({ pairs: [ompLocal], default_pair: ompLocal });
});

describe("Tonight list", () => {
  const data = (over: Partial<NightTonight> = {}): NightTonight => ({
    config: cfg,
    entries: [
      entry(),
      entry({ task_id: "t-2", title: "Refactor upload worker", state: "waiting", reason: "lane_busy" }),
      entry({ task_id: "t-3", title: "Docs", state: "started", night: "2026-09-24", run: mkRun({ state: "running" }) }),
    ],
    last_report: null,
    ...over,
  });

  it("lists tonight in order with window, pair and a plain state", async () => {
    wrap(<TonightListView data={data()} />);
    expect(screen.getByTestId("tonight-window")).toHaveTextContent("22:00–06:00 · Europe/Berlin");
    const rows = screen.getAllByTestId(/^tonight-row-/);
    expect(rows.map((r) => r.getAttribute("data-testid"))).toEqual(["tonight-row-t-1", "tonight-row-t-2", "tonight-row-t-3"]);
    expect(within(rows[0]).getByText(/omp · /)).toHaveTextContent("Queued");
    expect(rows[1]).toHaveTextContent("Waiting · another head uses the box");
    expect(rows[2]).toHaveTextContent("Running");
    // a started run cannot be removed from tonight (stop it on the head card)
    expect(within(rows[2]).queryByRole("button")).toBeNull();
    await waitFor(() => expect(rows[0]).toHaveTextContent("omp · GLM local"));
    expect(screen.queryByTestId("tonight-off")).toBeNull();
  });

  it("warns when the night shift is off", () => {
    wrap(<TonightListView data={data({ config: { ...cfg, enabled: false } })} />);
    expect(screen.getByTestId("tonight-off")).toHaveTextContent("The night shift is off — marked tasks will not start.");
    expect(within(screen.getByTestId("tonight-off")).getByRole("link", { name: "Settings" })).toHaveAttribute(
      "href",
      "/settings?section=night-shift",
    );
  });

  it("× removes the mark; the target is 44 px on phones", async () => {
    const unmark = vi.spyOn(api.nightShift, "unmark").mockResolvedValue({ mark: null });
    wrap(<TonightListView data={data()} />);
    const remove = screen.getByRole("button", { name: "Remove “Fix flaky retry test” from tonight" });
    expect(remove.className).toContain("min-h-[44px]");
    expect(remove.className).toContain("min-w-[44px]");
    await userEvent.click(remove);
    await waitFor(() => expect(unmark).toHaveBeenCalledWith("t-1"));
  });

  it("a row opens the task in place when the list can, else it links", async () => {
    const open = vi.fn(() => true);
    wrap(<TonightListView data={data()} onOpenTask={open} />);
    const link = within(screen.getByTestId("tonight-row-t-1")).getByRole("link");
    expect(link).toHaveAttribute("href", "/tasks?task=t-1");
    await userEvent.click(link);
    expect(open).toHaveBeenCalledWith("t-1");
  });

  it("shows last night's report in one line", () => {
    wrap(
      <TonightListView
        data={data({
          entries: [],
          last_report: {
            night: "2026-09-23",
            sent_at: "2026-09-24T04:00:00Z",
            delivered: false,
            entries: [
              { task_id: "a", title: "A", started: true, category: "passed" },
              { task_id: "b", title: "B", started: true, category: "failed" },
              { task_id: "c", title: "C", started: true, category: "needs_you" },
            ],
          },
        })}
      />,
    );
    expect(screen.getByTestId("tonight-last-report")).toHaveTextContent(
      "Last night (2026-09-23): 1 passed · 1 failed · 1 needs you · 0 blocked · report not delivered",
    );
    expect(screen.getByText("Nothing is marked for tonight.")).toBeInTheDocument();
  });

  it("renders nothing with no marks and no report", () => {
    const { container } = wrap(<TonightListView data={data({ entries: [] })} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("Run tonight on the task detail", () => {
  beforeEach(() => {
    vi.spyOn(api.nightShift, "config").mockResolvedValue(cfg);
  });

  it("switching on marks the task with the local default pair", async () => {
    vi.spyOn(api.nightShift, "getMark").mockResolvedValue({ mark: null });
    const mark = vi.spyOn(api.nightShift, "mark").mockResolvedValue({ mark: entry() });
    wrap(<NightShiftToggle taskId="t-1" />);
    await waitFor(() => expect(screen.getByTestId("night-line")).toHaveTextContent("tonight's window (22:00–06:00)"));
    const sw = screen.getByRole("switch", { name: "Run tonight" });
    expect(sw).toHaveAttribute("aria-checked", "false");
    expect(sw.className).toContain("min-h-[44px]");
    await userEvent.click(sw);
    await waitFor(() => expect(mark).toHaveBeenCalledWith("t-1", { harness: "omp", runtime_slug: "glm-local" }));
  });

  it("a marked task shows its pair and switching off unmarks it", async () => {
    vi.spyOn(api.nightShift, "getMark").mockResolvedValue({ mark: entry() });
    const unmark = vi.spyOn(api.nightShift, "unmark").mockResolvedValue({ mark: null });
    wrap(<NightShiftToggle taskId="t-1" />);
    const sw = await screen.findByRole("switch", { name: "Run tonight" });
    await waitFor(() => expect(sw).toHaveAttribute("aria-checked", "true"));
    await waitFor(() => expect(screen.getByTestId("night-line")).toHaveTextContent("Queued · omp · GLM local"));
    expect(screen.getByTestId("head-pair-trigger")).toBeInTheDocument();
    await userEvent.click(sw);
    await waitFor(() => expect(unmark).toHaveBeenCalledWith("t-1"));
  });

  it("once started tonight the switch is locked", async () => {
    vi.spyOn(api.nightShift, "getMark").mockResolvedValue({ mark: entry({ state: "started" }) });
    wrap(<NightShiftToggle taskId="t-1" />);
    await waitFor(() => expect(screen.getByTestId("night-line")).toHaveTextContent("Started tonight"));
    expect(screen.getByRole("switch", { name: "Run tonight" })).toBeDisabled();
    expect(screen.queryByTestId("head-pair-trigger")).toBeNull();
  });

  it("the report line says 'not delivered', not 'no report channel'", () => {
    wrap(
      <TonightListView
        data={{ config: cfg, entries: [], last_report: { night: "2026-09-24", sent_at: "x", delivered: false, entries: [] } }}
      />,
    );
    expect(screen.getByText(/report not delivered/)).toBeInTheDocument();
    expect(screen.queryByText(/no report channel/)).toBeNull();
  });

  it("a card someone works on offers no switch", async () => {
    vi.spyOn(api.nightShift, "getMark").mockResolvedValue({ mark: null });
    const { container } = wrap(<NightShiftToggle taskId="t-1" canMark={false} />);
    await waitFor(() => expect(api.nightShift.getMark).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 0));
    expect(container.querySelector("[data-testid='night-toggle']")).toBeNull();
  });

  it("a mark on a card that was taken meanwhile stays visible, says why and can be removed", async () => {
    vi.spyOn(api.nightShift, "getMark").mockResolvedValue({ mark: entry({ state: "skipped", reason: "task_moved" }) });
    const unmark = vi.spyOn(api.nightShift, "unmark").mockResolvedValue({ mark: null });
    wrap(<NightShiftToggle taskId="t-1" canMark={false} />);
    await waitFor(() => expect(screen.getByTestId("night-line")).toHaveTextContent("someone else took the task"));
    await userEvent.click(screen.getByRole("switch", { name: "Run tonight" }));
    await waitFor(() => expect(unmark).toHaveBeenCalledWith("t-1"));
  });

  it("warns on a marked task while the night shift is off", async () => {
    vi.spyOn(api.nightShift, "config").mockResolvedValue({ ...cfg, enabled: false });
    vi.spyOn(api.nightShift, "getMark").mockResolvedValue({ mark: entry() });
    wrap(<NightShiftToggle taskId="t-1" />);
    expect(await screen.findByTestId("night-off-warning")).toHaveTextContent("The night shift is off");
  });
});

describe("Settings → Night shift", () => {
  it("edits the window and saves only the form values", async () => {
    vi.spyOn(api.nightShift, "config").mockResolvedValue(cfg);
    const save = vi.spyOn(api.nightShift, "saveConfig").mockResolvedValue({ ...cfg, start: "23:00" });
    wrap(<NightShiftTab />);
    const start = await screen.findByTestId("night-start");
    expect(screen.getByTestId("night-save")).toBeDisabled(); // nothing changed yet
    await userEvent.clear(start);
    await userEvent.type(start, "23:00");
    await userEvent.click(screen.getByTestId("night-save"));
    await waitFor(() =>
      expect(save).toHaveBeenCalledWith({ enabled: true, start: "23:00", end: "06:00", timezone: "Europe/Berlin", cloud_share: 30 }),
    );
  });

  it("refuses start == end and a share above 100 before saving", async () => {
    vi.spyOn(api.nightShift, "config").mockResolvedValue(cfg);
    const save = vi.spyOn(api.nightShift, "saveConfig");
    wrap(<NightShiftTab />);
    const end = await screen.findByTestId("night-end");
    await userEvent.clear(end);
    await userEvent.type(end, "22:00");
    expect(screen.getByTestId("night-invalid-end")).toHaveTextContent("not the same time as the start");
    const share = screen.getByTestId("night-cloud-share");
    await userEvent.clear(share);
    await userEvent.type(share, "150");
    expect(screen.getByTestId("night-invalid-cloud_share")).toBeInTheDocument();
    expect(screen.getByTestId("night-save")).toBeDisabled();
    expect(save).not.toHaveBeenCalled();
  });

  it("the on/off switch has a 44 px target and inputs are 16 px on phones", async () => {
    vi.spyOn(api.nightShift, "config").mockResolvedValue(cfg);
    wrap(<NightShiftTab />);
    const sw = await screen.findByRole("switch", { name: "Night shift on" });
    expect(sw.className).toContain("min-h-[44px]");
    expect(screen.getByTestId("night-start").className).toContain("text-base");
    expect(screen.getByTestId("night-timezone").className).toContain("min-h-[44px]");
  });

  it("says so when heads are switched off", async () => {
    vi.spyOn(api.nightShift, "config").mockRejectedValue(new Error('API 404: {"detail":{"code":"heads_disabled"}}'));
    wrap(<NightShiftTab />);
    expect(await screen.findByTestId("night-settings-heads-off")).toHaveTextContent("Heads are switched off");
  });
});

/**
 * ActionBar — Stop-Dispatch-Gate (Spec §5, Form verifiziert gegen den PR-2-
 * Review-Vorlauf 06.09.2026: `detail:{code:"agent_busy", agents:[...]}`): ein
 * beschäftigter Agent lässt den ersten Stop mit 409 scheitern, die Bühne
 * zeigt eine inline Bestätigung ("Stop anyway") statt window.confirm, ein
 * Klick darauf wiederholt den Stop mit force=true.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ActionBar } from "../ActionBar";
import { api } from "@/lib/api";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return { ...render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>), qc };
}

describe("ActionBar — Stop dispatch gate", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.hosts, "recipes").mockResolvedValue([]);
  });

  it("409 with {code:'agent_busy', agents:[...]} → inline confirmation, no window.confirm", async () => {
    const confirmSpy = vi.spyOn(window, "confirm");
    vi.spyOn(api.runtimes, "stop").mockImplementationOnce(() =>
      Promise.reject(
        new Error(
          'API 409: {"detail":{"code":"agent_busy","agents":[{"name":"Alpha","slug":"alpha","task_id":"t-1"}]}}'
        )
      )
    );

    renderWithQuery(
      <ActionBar hostId="spark" hostName="spark" servingName="Qwen3.8" runtimeId="rt-1" onOpenCockpit={() => {}} />
    );

    const stopBtn = await screen.findByTestId("stop-runtime");
    await act(async () => { stopBtn.click(); });

    await waitFor(() => expect(screen.getByTestId("stop-conflict-row")).toBeTruthy());
    expect(screen.getByText(/Alpha/)).toBeTruthy();
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("clicking 'Stop anyway' retries with force:true", async () => {
    const stopSpy = vi
      .spyOn(api.runtimes, "stop")
      .mockImplementationOnce(() =>
        Promise.reject(
          new Error(
            'API 409: {"detail":{"code":"agent_busy","agents":[{"name":"Alpha","slug":"alpha","task_id":"t-1"}]}}'
          )
        )
      )
      .mockImplementationOnce(() => Promise.resolve({ ok: true, message: "stopped" }));

    renderWithQuery(
      <ActionBar hostId="spark" hostName="spark" servingName="Qwen3.8" runtimeId="rt-1" onOpenCockpit={() => {}} />
    );

    const stopBtn = await screen.findByTestId("stop-runtime");
    await act(async () => { stopBtn.click(); });
    const anywayBtn = await screen.findByTestId("stop-anyway");
    await act(async () => { anywayBtn.click(); });

    await waitFor(() => expect(stopSpy).toHaveBeenCalledTimes(2));
    expect(stopSpy.mock.calls[0]).toEqual(["rt-1", { force: false }]);
    expect(stopSpy.mock.calls[1]).toEqual(["rt-1", { force: true }]);
  });

  it("gear button opens the cockpit callback", async () => {
    const onOpen = vi.fn();
    renderWithQuery(
      <ActionBar hostId="spark" hostName="spark" servingName="Qwen3.8" runtimeId="rt-1" onOpenCockpit={onOpen} />
    );
    const gear = await screen.findByLabelText("Open cockpit");
    await act(async () => { gear.click(); });
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("a 409 without the agent_busy shape falls through to the plain error sentence", async () => {
    vi.spyOn(api.runtimes, "stop").mockImplementationOnce(() =>
      Promise.reject(new Error('API 409: {"detail":"some other conflict"}'))
    );
    renderWithQuery(
      <ActionBar hostId="spark" hostName="spark" servingName="Qwen3.8" runtimeId="rt-1" onOpenCockpit={() => {}} />
    );
    const stopBtn = await screen.findByTestId("stop-runtime");
    await act(async () => { stopBtn.click(); });
    expect(await screen.findByText(/Stop failed:/)).toBeTruthy();
    expect(screen.queryByTestId("stop-conflict-row")).not.toBeInTheDocument();
  });

  it("the primary switch button reads 'Switch model', not the running recipe name (Live-Sichtprüfung 06.09.2026)", async () => {
    renderWithQuery(
      <ActionBar hostId="spark" hostName="spark" servingName="Qwen3.8 Flash Next" runtimeId="rt-1" onOpenCockpit={() => {}} />
    );
    const trigger = await screen.findByTestId("recipe-dropdown-trigger");
    expect(trigger).toHaveTextContent("Switch model");
    expect(trigger).not.toHaveTextContent("Qwen3.8 Flash Next");
  });

  it("trouble variant shows 'Other model' next to Restart now and Stop", async () => {
    renderWithQuery(
      <ActionBar hostId="spark" hostName="spark" servingName="Qwen3.8" runtimeId="rt-1" variant="trouble" onOpenCockpit={() => {}} />
    );
    expect(await screen.findByTestId("restart-now")).toBeInTheDocument();
    expect(screen.getByTestId("recipe-dropdown-trigger")).toHaveTextContent("Other model");
    expect(screen.getByTestId("stop-runtime")).toBeInTheDocument();
  });

  // Review #438 Fund 5+6 (06.09.2026): mobile full-width stacking (Spec §2
  // Zone 4 "Handy: primär volle Breite, Zahnrad daneben, Stop volle Breite
  // darunter") lives in the `.stage-actions`/`.stage-actions-row2` container-
  // query rules (globals.css), which jsdom cannot evaluate — this asserts
  // the CSS hooks are actually wired up in the markup, not that the
  // container query itself resolves (that needs a real browser/Playwright).
  it("wires the mobile-stacking CSS hooks (stage-actions / stage-actions-row2)", async () => {
    renderWithQuery(
      <ActionBar hostId="spark" hostName="spark" servingName="Qwen3.8" runtimeId="rt-1" onOpenCockpit={() => {}} />
    );
    const trigger = await screen.findByTestId("recipe-dropdown-trigger");
    // HostRecipeSwitcher's DOM root (`display:contents`) still nests the
    // button one level deep in the tree even though it renders as if it
    // weren't there — `closest()` finds the real layout parent regardless.
    expect(trigger.closest(".stage-actions")).toBeTruthy();
    expect(screen.getByTestId("stop-runtime").parentElement).toHaveClass("stage-actions-row2");
  });

  it("the gear button carries the 44px-mobile CSS hook (stage-actions-gear)", async () => {
    renderWithQuery(
      <ActionBar hostId="spark" hostName="spark" servingName="Qwen3.8" runtimeId="rt-1" onOpenCockpit={() => {}} />
    );
    const gear = await screen.findByLabelText("Open cockpit");
    expect(gear).toHaveClass("stage-actions-gear");
    expect(gear).toHaveClass("w-11");
    expect(gear).toHaveClass("h-11");
  });

  it("restart button says in its tooltip whether both boxes come along", async () => {
    const solo = renderWithQuery(
      <ActionBar hostId="spark" hostName="spark" servingName="Qwen3.8" runtimeId="rt-1" variant="trouble" onOpenCockpit={() => {}} />
    );
    expect((await screen.findByTestId("restart-now")).getAttribute("title")).toBe(
      "Restarts the model running on this box."
    );
    solo.unmount();

    renderWithQuery(
      <ActionBar hostId="spark" hostName="spark" servingName="Qwen3.8" runtimeId="rt-1" variant="trouble" multiNode onOpenCockpit={() => {}} />
    );
    expect((await screen.findByTestId("restart-now")).getAttribute("title")).toBe(
      "Multi-node runtime: both boxes are stopped and started together."
    );
  });
});

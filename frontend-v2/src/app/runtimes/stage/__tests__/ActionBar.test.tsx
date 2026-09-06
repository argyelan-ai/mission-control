/**
 * ActionBar — Stop-Dispatch-Gate (Spec §5, Assumption zur 409-Form): ein
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

  it("409 with {agent, task} → inline confirmation, no window.confirm", async () => {
    const confirmSpy = vi.spyOn(window, "confirm");
    vi.spyOn(api.runtimes, "stop").mockImplementationOnce(() =>
      Promise.reject(new Error('API 409: {"detail":{"agent":"Sparky","task":"Fix the parser"}}'))
    );

    renderWithQuery(
      <ActionBar hostId="spark" hostName="spark" servingName="Qwen3.8" runtimeId="rt-1" onOpenCockpit={() => {}} />
    );

    const stopBtn = await screen.findByTestId("stop-runtime");
    await act(async () => { stopBtn.click(); });

    await waitFor(() => expect(screen.getByTestId("stop-conflict-row")).toBeTruthy());
    expect(screen.getByText(/Sparky/)).toBeTruthy();
    expect(screen.getByText(/Fix the parser/)).toBeTruthy();
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("clicking 'Stop anyway' retries with force:true", async () => {
    const stopSpy = vi
      .spyOn(api.runtimes, "stop")
      .mockImplementationOnce(() => Promise.reject(new Error('API 409: {"detail":{"agent":"Sparky","task":"Fix"}}')))
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
});

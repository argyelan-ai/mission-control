/**
 * CliToolsSection — vitest (Feature „CLI-Tool-Updates", Task 8).
 *
 * Coverage:
 *   1. update_available → „Update <latest>"-Badge sichtbar
 *   2. kein update_available → kein Badge
 *   3. Update-Button → Modal öffnet → Bestätigen ruft api.cliTools.update(tool)
 *   4. failed-Progress rendert den Fehlergrund
 *   5. busy-Agent-Pill ist gedimmt + trägt den „nach Task-Ende"-Tooltip
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CliToolsSection } from "../CliToolsSection";
import { api } from "@/lib/api";
import type { CliToolStatus, CliToolsResponse, CliUpdateProgress } from "@/lib/types";

const mkTool = (overrides: Partial<CliToolStatus> = {}): CliToolStatus => ({
  tool: "openclaude",
  image: "mc-agent-base:latest",
  installed: "2026.4.10",
  target: "2026.4.10",
  latest: "2026.5.01",
  update_available: true,
  checked_at: new Date().toISOString(),
  agents_affected: [{ id: "a1", name: "Sparky", busy: false }],
  build_state: null,
  ...overrides,
});

const mkList = (tools: CliToolStatus[]): CliToolsResponse => ({ tools });

const idleProgress: CliUpdateProgress = { phase: "idle" };

function renderSection() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <CliToolsSection />
    </QueryClientProvider>,
  );
}

describe("CliToolsSection", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.cliTools, "updateStatus").mockResolvedValue(idleProgress);
  });

  it("shows the update badge with the latest version when an update is available", async () => {
    vi.spyOn(api.cliTools, "list").mockResolvedValue(mkList([mkTool()]));
    renderSection();
    await waitFor(() => expect(screen.getByText(/Update 2026\.5\.01/)).toBeInTheDocument());
  });

  it("shows no update badge when the tool is up to date", async () => {
    vi.spyOn(api.cliTools, "list").mockResolvedValue(
      mkList([mkTool({ update_available: false, latest: "2026.4.10" })]),
    );
    renderSection();
    await waitFor(() => expect(screen.getByText("openclaude")).toBeInTheDocument());
    expect(screen.queryByText(/^Update /)).not.toBeInTheDocument();
  });

  it("opens the confirm modal and calls api.cliTools.update on confirm", async () => {
    vi.spyOn(api.cliTools, "list").mockResolvedValue(mkList([mkTool()]));
    const updateSpy = vi
      .spyOn(api.cliTools, "update")
      .mockResolvedValue({ status: "accepted" });
    renderSection();

    await waitFor(() => expect(screen.getByText(/Update 2026\.5\.01/)).toBeInTheDocument());
    await userEvent.click(screen.getByText(/Update 2026\.5\.01/));

    // Modal open — manifest-commit hint + confirm button present
    await waitFor(() => expect(screen.getByText(/Manifest-Änderung/)).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /Jetzt aktualisieren/ }));

    await waitFor(() => expect(updateSpy).toHaveBeenCalledWith("openclaude"));
  });

  it("renders the failure reason when the update status reports a failed phase", async () => {
    vi.spyOn(api.cliTools, "list").mockResolvedValue(mkList([mkTool()]));
    vi.spyOn(api.cliTools, "update").mockResolvedValue({ status: "accepted" });
    vi.spyOn(api.cliTools, "updateStatus").mockResolvedValue({
      phase: "failed",
      tool: "openclaude",
      error: "docker build exited 1",
    });
    renderSection();

    await waitFor(() => expect(screen.getByText(/Update 2026\.5\.01/)).toBeInTheDocument());
    await userEvent.click(screen.getByText(/Update 2026\.5\.01/));
    await userEvent.click(screen.getByRole("button", { name: /Jetzt aktualisieren/ }));

    await waitFor(() =>
      expect(screen.getByTestId("cli-update-error")).toHaveTextContent("docker build exited 1"),
    );
  });

  it("dims a busy agent pill and gives it the after-task tooltip", async () => {
    vi.spyOn(api.cliTools, "list").mockResolvedValue(
      mkList([mkTool({ agents_affected: [{ id: "a1", name: "Sparky", busy: true }] })]),
    );
    renderSection();

    const pill = await screen.findByTitle(/folgt nach Task-Ende/);
    expect(pill).toHaveTextContent("Sparky");
    expect(pill).toHaveStyle({ opacity: "0.45" });
  });
});

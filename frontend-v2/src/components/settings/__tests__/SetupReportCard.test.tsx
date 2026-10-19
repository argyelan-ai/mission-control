/**
 * "Copy setup report" (Welle 3, PR 3-4; Settings → About). The report itself
 * is built and tested in lib/setupReport.ts — this covers the button: it
 * copies to the clipboard and tells the operator it worked (or didn't).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api } from "@/lib/api";
import { useNotificationStore } from "@/lib/store";
import { SetupReportCard } from "../SetupReportCard";

beforeEach(() => {
  vi.restoreAllMocks();
  useNotificationStore.setState({ notifications: [] });
  vi.spyOn(api.system, "version").mockResolvedValue({
    current: "0.9.0",
    latest: null,
    release_url: null,
    update_available: false,
  });
  vi.spyOn(api.system, "status").mockResolvedValue({
    status: "healthy",
    components: {
      database: { status: "ok" },
      redis: { status: "ok" },
      gateway: { status: "ok" },
      watchdog: { status: "ok" },
    },
    resources: null,
    agents: { total: 1, online: 1, offline: 0 },
    uptime_seconds: 60,
    version: "0.9.0",
  } as any);
  vi.spyOn(api.configChecks, "list").mockResolvedValue({ kinds: [], cost_hints: {} as any, items: [] });
  vi.spyOn(api.settings, "get").mockRejectedValue(new Error("API 404: Setting not found"));
});

function stubClipboard() {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.assign(navigator, { clipboard: { writeText } });
  return writeText;
}

describe("SetupReportCard", () => {
  it("copies the setup report to the clipboard and confirms it", async () => {
    const writeText = stubClipboard();
    render(<SetupReportCard />);

    await userEvent.click(screen.getByRole("button", { name: "Copy setup report" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const report = writeText.mock.calls[0][0] as string;
    expect(report).toContain("Mission Control setup report");
    expect(report).toContain("Version: 0.9.0");

    await waitFor(() =>
      expect(useNotificationStore.getState().notifications.some((n) => n.message === "Setup report copied")).toBe(
        true,
      ),
    );
  });

  it("tells the operator when the copy itself fails", async () => {
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });
    render(<SetupReportCard />);

    await userEvent.click(screen.getByRole("button", { name: "Copy setup report" }));

    await waitFor(() =>
      expect(
        useNotificationStore.getState().notifications.some((n) => n.message === "Could not copy the report"),
      ).toBe(true),
    );
  });
});

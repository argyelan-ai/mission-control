/**
 * HeadDiskWarning — "~/.mc/heads" nearly full on /runtimes (bauplan
 * `heads-sichtbar` PR 4 §5, `GET /heads/cleanup`'s `warn_low_disk`).
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { HeadDiskWarning } from "../HeadOccupancy";

const mocks = vi.hoisted(() => ({
  cleanup: vi.fn(),
  occupancy: vi.fn(async () => ({ boxes: {} })),
}));

vi.mock("@/lib/api", () => ({
  api: { heads: { cleanup: mocks.cleanup, occupancy: mocks.occupancy } },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("HeadDiskWarning", () => {
  it("renders nothing while there is no report yet", async () => {
    mocks.cleanup.mockResolvedValue({ report: null });
    render(<HeadDiskWarning />, { wrapper });
    await waitFor(() => expect(mocks.occupancy).toHaveBeenCalled());
    expect(screen.queryByTestId("head-disk-warning")).not.toBeInTheDocument();
  });

  // Sabotage: a report that is merely PRESENT but says the disk is fine
  // must still render nothing — removing the `!report?.warn_low_disk`
  // gate (checking only `!report`) would show this banner permanently
  // once ANY report exists, the opposite of "hidden whenever there is
  // nothing to warn about" (K4).
  it("renders nothing while the report says the disk is fine", async () => {
    mocks.cleanup.mockResolvedValue({ report: { warn_low_disk: false, free_bytes: 200 * 1024 ** 3, runs: [] } });
    render(<HeadDiskWarning />, { wrapper });
    await waitFor(() => expect(mocks.occupancy).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByTestId("head-disk-warning")).not.toBeInTheDocument();
  });

  it("shows the free GB figure once warn_low_disk is true", async () => {
    mocks.cleanup.mockResolvedValue({ report: { warn_low_disk: true, free_bytes: 12 * 1024 ** 3, runs: [] } });
    render(<HeadDiskWarning />, { wrapper });
    await waitFor(() => expect(screen.getByTestId("head-disk-warning")).toBeInTheDocument());
    expect(screen.getByTestId("head-disk-warning")).toHaveTextContent("12");
  });

  it("falls back to the no-number copy when free_bytes is null", async () => {
    mocks.cleanup.mockResolvedValue({ report: { warn_low_disk: true, free_bytes: null, runs: [] } });
    render(<HeadDiskWarning />, { wrapper });
    await waitFor(() => expect(screen.getByTestId("head-disk-warning")).toBeInTheDocument());
    expect(screen.getByTestId("head-disk-warning")).toHaveTextContent("Disk nearly full. Head working copies and caches may need cleanup.");
  });
});

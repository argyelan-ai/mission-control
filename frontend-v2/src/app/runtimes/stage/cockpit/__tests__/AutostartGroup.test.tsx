/**
 * AutostartGroup — Team-Lead-Fund 06.09.2026: die Fusszeile darf NIE den
 * vollen `last_result`-Meldungssatz zeigen (das ist genau der Meldungstext,
 * den die Spec für die Karte verbietet, und im Cockpit zu lang ist). Sie
 * zeigt nur Zeit + ein erkanntes Ergebnis-Wort (ok/failed), sonst nur die
 * Zeit.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AutostartGroup } from "../AutostartGroup";
import { api } from "@/lib/api";
import type { HostAutostartStatus } from "@/lib/types";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function makeStatus(over: Partial<HostAutostartStatus> = {}): HostAutostartStatus {
  return {
    host_id: "spark", enabled: true, recipe_slug: "qwen38-flash-next", recipe_display_name: "Qwen3.8 Flash Next",
    role: "head", via_head: null, last_attempt_at: null, last_result: null,
    ...over,
  };
}

beforeEach(() => vi.restoreAllMocks());

function expectedWhen(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

describe("AutostartGroup — last-attempt line", () => {
  it("never renders the full last_result message sentence", async () => {
    vi.spyOn(api.hosts, "autostart").mockResolvedValue(
      makeStatus({
        last_attempt_at: "2026-09-05T07:47:00Z",
        last_result: "Gestartet — DeepSeek V4 Flash Vision · Gewichte laden dauert · Logs: ~/.cache/mc/runtime-launch-5d04fc42.log",
      })
    );
    renderWithQuery(<AutostartGroup hostId="spark" isAdmin />);
    const line = await screen.findByTestId("cockpit-autostart-last-attempt");
    expect(line.textContent).not.toMatch(/Gewichte laden|Logs:|cache/i);
  });

  it("a 'started' result renders as one mono line: time · ok", async () => {
    vi.spyOn(api.hosts, "autostart").mockResolvedValue(
      makeStatus({ last_attempt_at: "2026-09-05T07:47:00Z", last_result: "Gestartet 05.09. 07:47" })
    );
    renderWithQuery(<AutostartGroup hostId="spark" isAdmin />);
    const line = await screen.findByTestId("cockpit-autostart-last-attempt");
    expect(line.textContent).toBe(`last attempt ${expectedWhen("2026-09-05T07:47:00Z")} · ok`);
  });

  it("a failure result renders as: time · failed", async () => {
    vi.spyOn(api.hosts, "autostart").mockResolvedValue(
      makeStatus({ last_attempt_at: "2026-09-04T23:12:00Z", last_result: "Failed · memory gate, GLM worker still loaded" })
    );
    renderWithQuery(<AutostartGroup hostId="spark" isAdmin />);
    const line = await screen.findByTestId("cockpit-autostart-last-attempt");
    expect(line.textContent).toBe(`last attempt ${expectedWhen("2026-09-04T23:12:00Z")} · failed`);
  });

  it("an unrecognizable result word falls back to time only", async () => {
    vi.spyOn(api.hosts, "autostart").mockResolvedValue(
      makeStatus({ last_attempt_at: "2026-09-05T07:47:00Z", last_result: "something ambiguous happened" })
    );
    renderWithQuery(<AutostartGroup hostId="spark" isAdmin />);
    const line = await screen.findByTestId("cockpit-autostart-last-attempt");
    expect(line.textContent).toBe(`last attempt ${expectedWhen("2026-09-05T07:47:00Z")}`);
  });

  it("no last_attempt_at at all: no line is rendered", async () => {
    vi.spyOn(api.hosts, "autostart").mockResolvedValue(makeStatus({ last_attempt_at: null, last_result: null }));
    renderWithQuery(<AutostartGroup hostId="spark" isAdmin />);
    await waitFor(() => expect(screen.getByTestId("cockpit-autostart-model")).toBeInTheDocument());
    expect(screen.queryByTestId("cockpit-autostart-last-attempt")).not.toBeInTheDocument();
  });
});

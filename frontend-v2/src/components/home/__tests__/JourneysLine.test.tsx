/** Home → last night's journey tests, one line (E6, docs/journeys.md). */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import en from "../../../../messages/en.json";
import de from "../../../../messages/de.json";
import { api } from "@/lib/api";
import { journeysReason, type JourneysResult } from "@/lib/journeys";
import { failedJourneyCount, JourneysLine, JourneysLineView, useFailedJourneys } from "../JourneysLine";
import { useAppStore, type AuthUser } from "@/lib/store";

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const mk = (over: Partial<JourneysResult> = {}): JourneysResult => ({
  status: "green",
  finished_at: new Date(Date.now() - 5 * 3600_000).toISOString(),
  commit: "abc1234",
  total: 3,
  passed: 3,
  failed: [],
  known_gaps: 0,
  reason: null,
  ...over,
});

const line = () => screen.getByTestId("journeys-line");

describe("JourneysLineView", () => {
  it("green: passed of total and when", () => {
    wrap(<JourneysLineView result={mk()} />);
    expect(line()).toHaveTextContent("Journey tests: 3 of 3 passed · 5 h ago");
    expect(line()).toHaveAttribute("data-status", "green");
  });

  // ICU plurals are not emulated by the test i18n mock (src/test-setup.ts),
  // so the plural parts are checked by their key, not their wording.
  it("green with pinned gaps adds the gap count", () => {
    const { unmount } = wrap(<JourneysLineView result={mk({ known_gaps: 1 })} />);
    expect(line()).toHaveTextContent(/^Journey tests: 3 of 3 passed, .*known gap/);
    unmount();
    wrap(<JourneysLineView result={mk()} />);
    expect(line()).not.toHaveTextContent("known gap");
  });

  it("red names the failed journeys", () => {
    wrap(<JourneysLineView result={mk({ status: "red", passed: 1, failed: ["J-phone-needs-you", "J-usage"] })} />);
    expect(line()).toHaveTextContent("Journey tests: Answer from phone, Cost & local share failed · 5 h ago");
    expect(line()).not.toHaveTextContent("J-usage");
    expect(line()).toHaveAttribute("data-status", "red");
  });

  it("red falls back to the id for a journey without a name yet", () => {
    wrap(<JourneysLineView result={mk({ status: "red", passed: 2, failed: ["J-brand-new"] })} />);
    expect(line()).toHaveTextContent("Journey tests: J-brand-new failed");
  });

  it("skipped and error translate the runner reason, never show it raw", () => {
    const { unmount } = wrap(
      <JourneysLineView result={mk({ status: "skipped", total: 0, passed: 0, reason: "head running (heartbeat in the last 3 min)" })} />,
    );
    expect(line()).toHaveTextContent("Journey tests skipped: a head was working");
    expect(line()).not.toHaveTextContent("heartbeat");
    unmount();
    wrap(<JourneysLineView result={mk({ status: "error", total: 0, passed: 0, reason: "aborted (exit 128)" })} />);
    expect(line()).toHaveTextContent("Journey tests did not finish");
    expect(line()).not.toHaveTextContent("exit 128");
  });
});

describe("journey names", () => {
  it("every journey in the product map has a name in EN and DE", () => {
    const map = readFileSync(resolve(process.cwd(), "../docs/produkt/landkarte.yaml"), "utf8");
    const ids = [...map.matchAll(/^\s*- id: (J-[a-z0-9-]+)/gm)].map((m) => m[1]);
    expect(ids.length).toBeGreaterThan(0);
    for (const names of [en.home.journeys.names, de.home.journeys.names]) {
      expect(Object.keys(names).sort()).toEqual([...ids].sort());
    }
  });
});

describe("journeysReason", () => {
  it("maps the runner texts it knows", () => {
    expect(journeysReason("deploy running (deploy.lock)")).toBe("deploy");
    expect(journeysReason("disk: 12 GB free, need 20")).toBe("disk");
    expect(journeysReason("port 18080 is busy")).toBe("port");
    expect(journeysReason("time limit 1800s reached")).toBe("timeout");
    expect(journeysReason("something new")).toBeNull();
  });
});

describe("JourneysLine", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("loads the result from the backend", async () => {
    const spy = vi.spyOn(api.system, "journeys").mockResolvedValue({ result: mk() });
    wrap(<JourneysLine />);
    expect(await screen.findByTestId("journeys-line")).toBeInTheDocument();
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("renders nothing before the first run", async () => {
    const spy = vi.spyOn(api.system, "journeys").mockResolvedValue({ result: null });
    const { container } = wrap(<JourneysLine />);
    await vi.waitFor(() => expect(spy).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});

describe("failedJourneyCount — only a fresh red run waits on someone", () => {
  const now = Date.parse("2026-11-22T12:00:00Z");
  const hoursAgo = (h: number) => new Date(now - h * 3600_000).toISOString();

  it("counts a red run from last night", () => {
    expect(failedJourneyCount(mk({ status: "red", passed: 2, failed: ["J-a"], finished_at: hoursAgo(10) }), now)).toBe(1);
  });

  it("a red run older than one nightly cycle plus slack counts 0 (the dated Home line keeps it)", () => {
    expect(failedJourneyCount(mk({ status: "red", passed: 2, failed: ["J-a"], finished_at: hoursAgo(37) }), now)).toBe(0);
    expect(failedJourneyCount(mk({ status: "red", passed: 2, failed: ["J-a"], finished_at: "2020-01-01T00:00:00Z" }), now)).toBe(0);
  });

  it("a red run without a finish time counts 0 — its age is unknown", () => {
    expect(failedJourneyCount(mk({ status: "red", passed: 2, failed: ["J-a"], finished_at: null }), now)).toBe(0);
  });

  it("red then green clears the count", () => {
    expect(failedJourneyCount(mk({ status: "green", finished_at: hoursAgo(1) }), now)).toBe(0);
  });
});

describe("useFailedJourneys — the operator's signal, admins only", () => {
  beforeEach(() => vi.restoreAllMocks());
  const red = () => mk({ status: "red", passed: 2, failed: ["J-a"], finished_at: new Date(Date.now() - 3600_000).toISOString() });

  function Probe() {
    const { count } = useFailedJourneys();
    return <span data-testid="count">{count}</span>;
  }

  it("an admin sees a fresh red run counted", async () => {
    useAppStore.setState({ currentUser: { id: "u1", role: "admin" } as AuthUser });
    vi.spyOn(api.system, "journeys").mockResolvedValue({ result: red() });
    wrap(<Probe />);
    await vi.waitFor(() => expect(screen.getByTestId("count")).toHaveTextContent("1"));
  });

  it("a member never gets the journey count — nothing there to act on", async () => {
    useAppStore.setState({ currentUser: { id: "u2", role: "member" } as AuthUser });
    const spy = vi.spyOn(api.system, "journeys").mockResolvedValue({ result: red() });
    wrap(<Probe />);
    await vi.waitFor(() => expect(spy).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.getByTestId("count")).toHaveTextContent("0");
  });
});

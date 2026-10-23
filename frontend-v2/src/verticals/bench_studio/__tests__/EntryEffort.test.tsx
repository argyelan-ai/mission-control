import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { EntryEffort, EntryEffortPill, failedAtMaxTokens } from "../EntryEffort";
import type { BenchEntry } from "../types";

function entry(over: Partial<BenchEntry>): BenchEntry {
  return {
    id: "e-1", challenge_id: "ch-1", model_label: "M", source_kind: "spark", spark_model: "m",
    agent_id: null, display_tag: null, task_id: null, status: "generated", artifact_path: null,
    video_path: null, screenshot_path: null, metrics: {}, error: null, ...over,
  };
}

// Since round-2 Q2 the level is a pill in the entry head (one word from
// common.effortLevel); what went on the wire is the pill's tooltip; the line
// under the measurements only says what the run showed about thinking.

describe("EntryEffortPill (round-2 Q2)", () => {
  it("API entry: the level word, the wire key=value only in the tooltip", () => {
    render(
      <EntryEffortPill
        entry={entry({
          metrics: { effort_requested: "max", effort_sent: { key: "reasoning_effort", value: "max" } },
        })}
      />
    );
    const pill = screen.getByTestId("bench-entry-effort");
    expect(pill).toHaveTextContent(/^Max$/);
    expect(pill).toHaveAccessibleName("Effort: Max");
    expect(pill.getAttribute("title")).toBe("Sent with the request: reasoning_effort=max");
  });

  it("API entry on Default: says nothing was sent", () => {
    render(<EntryEffortPill entry={entry({ metrics: { effort_requested: "default", effort_sent: null } })} />);
    const pill = screen.getByTestId("bench-entry-effort");
    expect(pill).toHaveTextContent(/^Default$/);
    expect(pill.getAttribute("title")).toBe("Nothing sent — the server default applies.");
  });

  it("a running entry with a level: on its way, not 'nothing'", () => {
    render(<EntryEffortPill entry={entry({ status: "generating", metrics: { effort_requested: "max" } })} />);
    expect(screen.getByTestId("bench-entry-effort").getAttribute("title")).toBe("Is sent with the request.");
  });

  it("agent entry: the omp session level, default or changed in the tooltip", () => {
    const { unmount } = render(
      <EntryEffortPill
        entry={entry({
          source_kind: "agent",
          metrics: {
            effort_requested: "default",
            effort_observed: { source: "omp_session", level: "high", configured: null, changed: false, attempts: 1 },
          },
        })}
      />
    );
    expect(screen.getByTestId("bench-entry-effort")).toHaveTextContent(/^High$/);
    expect(screen.getByTestId("bench-entry-effort").getAttribute("title")).toMatch(/its default/);
    unmount();
    render(
      <EntryEffortPill
        entry={entry({
          source_kind: "agent",
          metrics: {
            effort_observed: { source: "omp_session", level: "low", configured: "low", changed: true, attempts: 1 },
          },
        })}
      />
    );
    expect(screen.getByTestId("bench-entry-effort")).toHaveTextContent(/^Low$/);
    expect(screen.getByTestId("bench-entry-effort").getAttribute("title")).not.toMatch(/default/);
  });

  it("old entries without a record show no pill", () => {
    const { container } = render(<EntryEffortPill entry={entry({ metrics: { duration_ms: 1000 } })} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("EntryEffort line (bench W0.6/W0.7)", () => {
  it("API entry: what the run showed about thinking, no level, no jargon", () => {
    render(
      <EntryEffort
        entry={entry({
          metrics: {
            effort_requested: "max",
            effort_sent: { key: "reasoning_effort", value: "max" },
            effort_observed: { thinking: "on", reasoning_tokens: 5120, reasoning_chars: 900 },
          },
        })}
      />
    );
    expect(screen.getByText("thinking on · 5,120 reasoning tok")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/reasoning_effort|Effort:/);
  });

  it("a running entry adds no line — pill and spinner already say it", () => {
    const { container } = render(
      <EntryEffort entry={entry({ status: "generating", metrics: { effort_requested: "max" } })} />
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("warns when the run stopped at max_tokens", () => {
    render(
      <EntryEffort
        entry={entry({ metrics: { effort_requested: "high", truncated: true, max_tokens: 32768 } })}
      />
    );
    expect(screen.getByText(/Stopped at max_tokens \(32768\)/)).toBeInTheDocument();
  });

  it("a FAILED run that stopped at max_tokens says so in the UI language", () => {
    const failed = entry({
      status: "failed",
      error: "generation failed: model returned no HTML content — stopped at max_tokens (16384) before the page",
      metrics: { effort_requested: "default", truncated: true, max_tokens: 16384 },
    });
    render(<EntryEffort entry={failed} />);
    expect(screen.getByText(/No page: stopped at max_tokens \(16384\)/)).toBeInTheDocument();
    expect(failedAtMaxTokens(failed)).toBe(true);
    expect(failedAtMaxTokens({ ...failed, metrics: { truncated: false } })).toBe(false);
    expect(failedAtMaxTokens({ ...failed, status: "generated" })).toBe(false);
  });

  it("an agent whose level is unknown says so", () => {
    render(
      <EntryEffort
        entry={entry({ source_kind: "agent", metrics: { effort_observed: { source: "unknown", reason: "x" } } })}
      />
    );
    expect(screen.getByText("Effort: not readable for this agent")).toBeInTheDocument();
  });

  it("old entries without a record render nothing", () => {
    const { container } = render(<EntryEffort entry={entry({ metrics: { duration_ms: 1000 } })} />);
    expect(container).toBeEmptyDOMElement();
  });
});

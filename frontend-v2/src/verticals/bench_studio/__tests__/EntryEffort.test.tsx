import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { EntryEffort } from "../EntryEffort";
import type { BenchEntry } from "../types";

function entry(over: Partial<BenchEntry>): BenchEntry {
  return {
    id: "e-1", challenge_id: "ch-1", model_label: "M", source_kind: "spark", spark_model: "m",
    agent_id: null, display_tag: null, task_id: null, status: "generated", artifact_path: null,
    video_path: null, screenshot_path: null, metrics: {}, error: null, ...over,
  };
}

describe("EntryEffort (bench W0.6/W0.7)", () => {
  it("API entry: requested level, the key=value sent and the thinking seen", () => {
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
    expect(
      screen.getByText("Effort: Max · sent reasoning_effort=max · thinking on · 5,120 reasoning tok")
    ).toBeInTheDocument();
  });

  it("API entry on Default says nothing was sent and what the server did", () => {
    render(
      <EntryEffort
        entry={entry({
          metrics: {
            effort_requested: "default",
            effort_sent: null,
            effort_observed: { thinking: "off", reasoning_tokens: null, reasoning_chars: 0 },
          },
        })}
      />
    );
    expect(screen.getByText("Effort: Default · sent: nothing · thinking off")).toBeInTheDocument();
  });

  it("warns when the run stopped at max_tokens", () => {
    render(
      <EntryEffort
        entry={entry({ metrics: { effort_requested: "high", truncated: true, max_tokens: 32768 } })}
      />
    );
    expect(screen.getByText(/Stopped at max_tokens \(32768\)/)).toBeInTheDocument();
  });

  it("agent entry prints the omp session default", () => {
    render(
      <EntryEffort
        entry={entry({
          source_kind: "agent",
          metrics: {
            effort_requested: "default",
            effort_observed: { source: "omp_session", level: "high", configured: null, changed: false, attempts: 1 },
          },
        })}
      />
    );
    expect(screen.getByText("omp session default: High")).toBeInTheDocument();
  });

  it("agent entry whose level changed is not called a default", () => {
    render(
      <EntryEffort
        entry={entry({
          source_kind: "agent",
          metrics: {
            effort_observed: { source: "omp_session", level: "low", configured: "low", changed: true, attempts: 1 },
          },
        })}
      />
    );
    expect(screen.getByText("omp session: Low")).toBeInTheDocument();
  });

  it("old entries without a record render nothing", () => {
    const { container } = render(<EntryEffort entry={entry({ metrics: { duration_ms: 1000 } })} />);
    expect(container).toBeEmptyDOMElement();
  });
});

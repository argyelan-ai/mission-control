/**
 * GroupStatusLine — vitest.
 *
 * Prüft die Vorrang-Reihenfolge der acht Regeln (die Reihenfolge IST das
 * Verhalten: „nicht verbunden" schlägt jeden fachlichen Status) sowie die
 * Kostenanzeige inklusive Budget-Warnschwelle.
 * Labels sind englisch, weil src/test-setup.ts next-intl gegen messages/en.json
 * auflöst.
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { C, STATUS_TEXT } from "@/lib/colors";
import { GroupStatusLine } from "./GroupStatusLine";
import { EMPTY_GROUP_STREAM_STATE } from "@/lib/groupTypes";
import type { GroupStreamState } from "@/lib/groupTypes";

const mkState = (patch: Partial<GroupStreamState> = {}): GroupStreamState => ({
  ...EMPTY_GROUP_STREAM_STATE,
  ...patch,
});

describe("GroupStatusLine", () => {
  it("reports the lost connection instead of a stale status", () => {
    render(
      <GroupStatusLine
        status="running"
        state={mkState({ roundNo: 2, pendingSpeakers: ["Ada"] })}
        connected={false}
      />
    );
    expect(screen.getByText("Status unknown — connection lost")).toBeInTheDocument();
    expect(screen.queryByText(/waiting for Ada/)).not.toBeInTheDocument();
  });

  it("says the group is waiting for the operator on waiting_gate", () => {
    render(<GroupStatusLine status="waiting_gate" state={mkState()} connected />);
    expect(screen.getByText("Waiting for you")).toBeInTheDocument();
  });

  it("shows the paused copy with the way out", () => {
    render(<GroupStatusLine status="paused" state={mkState()} connected />);
    expect(screen.getByText("Paused — “Resume” continues")).toBeInTheDocument();
  });

  it("shows the finished copy on done", () => {
    render(<GroupStatusLine status="done" state={mkState()} connected />);
    expect(screen.getByText("Finished")).toBeInTheDocument();
  });

  it("names the active speaker while a synthesis is being written", () => {
    render(
      <GroupStatusLine
        status="running"
        state={mkState({ activeSpeaker: "Ada", pendingSpeakers: ["Bob"], roundNo: 1 })}
        connected
      />
    );
    // Der aktive Sprecher schlägt die Warteliste — sonst behauptet die Zeile
    // ein Warten, obwohl gerade jemand schreibt.
    expect(screen.getByText("Ada is summarising…")).toBeInTheDocument();
  });

  it("lists the pending speakers with round and cap", () => {
    render(
      <GroupStatusLine
        status="running"
        state={mkState({ roundNo: 2, maxRounds: 5, pendingSpeakers: ["Ada", "Bob"] })}
        connected
      />
    );
    expect(screen.getByText("Round 2/5 — waiting for Ada, Bob")).toBeInTheDocument();
  });

  it("falls back to the between-rounds copy when nobody is pending", () => {
    render(<GroupStatusLine status="running" state={mkState({ roundNo: 3 })} connected />);
    expect(
      screen.getByText("Round 3 finished — the next one starts shortly")
    ).toBeInTheDocument();
  });

  it("shows the idle copy for a group without a running round", () => {
    render(<GroupStatusLine status="idle" state={mkState()} connected />);
    expect(screen.getByText("Ready — message the group or start a round")).toBeInTheDocument();
  });

  it("shows the idle copy for a draft group too", () => {
    render(<GroupStatusLine status="draft" state={mkState()} connected />);
    expect(screen.getByText("Ready — message the group or start a round")).toBeInTheDocument();
  });

  it("pulses only while something is actually happening", () => {
    const running = render(
      <GroupStatusLine
        status="running"
        state={mkState({ pendingSpeakers: ["Ada"], roundNo: 1, maxRounds: 3 })}
        connected
      />
    );
    expect(running.container.querySelector(".animate-ping")).not.toBeNull();

    const done = render(<GroupStatusLine status="done" state={mkState()} connected />);
    expect(done.container.querySelector(".animate-ping")).toBeNull();
  });

  it("hides the cost when nothing has been measured yet", () => {
    render(<GroupStatusLine status="idle" state={mkState()} connected budgetUsd={2} />);
    expect(screen.queryByText(/USD/)).not.toBeInTheDocument();
  });

  it("shows the spent amount once it is known", () => {
    render(<GroupStatusLine status="idle" state={mkState()} connected spentUsd={0.4} />);
    expect(screen.getByText("0.40 USD")).toBeInTheDocument();
  });

  it("shows spent against the budget when a budget is set", () => {
    render(
      <GroupStatusLine status="idle" state={mkState()} connected spentUsd={0.4} budgetUsd={2} />
    );
    expect(screen.getByText("0.40 / 2.00 USD")).toBeInTheDocument();
  });

  it("keeps the cost quiet well below the budget", () => {
    render(
      <GroupStatusLine status="idle" state={mkState()} connected spentUsd={0.4} budgetUsd={2} />
    );
    // Gegen den TOKEN prüfen, nie gegen einen abgeschriebenen Hex-Wert:
    // die Palette wurde im Shell-v4-Umbau aufgehellt, ein hartcodiertes
    // #666666 hätte den Test grundlos rot gemacht.
    expect(screen.getByText("0.40 / 2.00 USD")).toHaveStyle({ color: C.textDim });
  });

  it("turns the cost to the warning tone from 85% of the budget", () => {
    render(
      <GroupStatusLine status="idle" state={mkState()} connected spentUsd={1.7} budgetUsd={2} />
    );
    expect(screen.getByText("1.70 / 2.00 USD")).toHaveStyle({ color: STATUS_TEXT.warning });
  });

  it("announces itself politely so a screen reader picks up status changes", () => {
    const { container } = render(
      <GroupStatusLine status="idle" state={mkState()} connected />
    );
    expect(container.querySelector('[aria-live="polite"]')).not.toBeNull();
  });

  it("names a failed group instead of claiming it is ready", () => {
    // Wahrhaftigkeits-Regel: der Else-Zweig hätte hier „Ready …" behauptet.
    render(
      <GroupStatusLine status="failed" state={mkState()} connected />
    );
    expect(screen.getByText("Failed — check the round reports")).toBeInTheDocument();
    expect(screen.queryByText(/Ready/)).not.toBeInTheDocument();
  });
});

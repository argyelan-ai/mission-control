/**
 * ToolGroup vitest — the activity counting (thinking-only runs, error
 * aggregation), the i18n label built on top of it, and the collapse/expand
 * behaviour incl. its reaction to the detail level changing under a mounted
 * group.
 *
 * `summarizeActivity` only counts (no text) and is tested under the global
 * English mock like everything else here. `toolGroupLabel` is the part that
 * resolves the i18n strings — its own describe block below passes a `t` it
 * builds by hand so the exact EN wording (including ICU-plural "1 tool used"
 * vs. "3 tools used") is actually checked, not just the key it falls through
 * to under the plural-unaware global mock (review finding on PR #756 round
 * 2: this label used to be hardcoded German, shown unconditionally inside
 * the otherwise-English chat UI).
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ToolGroup, summarizeActivity, toolGroupLabel, type ActivityEvent } from "./ToolGroup";
import type { ThinkingEvent, ToolEvent } from "@/lib/chatTypes";
import { STATUS_TEXT } from "@/lib/colors";
import en from "../../../messages/en.json";
import de from "../../../messages/de.json";

// `vi.importActual` fetches the REAL next-intl module just for
// `createTranslator`, bypassing `src/test-setup.ts`'s global mock (which does
// not emulate `{count, plural, …}` at all) without changing how `ToolGroup`
// itself resolves `useTranslations` below — that still goes through the
// mock, same as every other component test in this file.
const { createTranslator } = await vi.importActual<typeof import("next-intl")>("next-intl");
// `toolGroupLabel` takes a plain `(key: string, …) => string` (so it works
// with any `t`, including the test mock elsewhere in this file); a REAL
// `createTranslator` result narrows `key` to this catalog's own literal
// union, which a generic `string` is correctly NOT assignable to — the
// same shape `toolGroupLabel` itself accepts, just typed more loosely.
type ToolGroupT = (key: string, values?: Record<string, string | number | Date>) => string;
const tEn = createTranslator({ locale: "en", messages: en, namespace: "sessions" }) as unknown as ToolGroupT;
const tDe = createTranslator({ locale: "de", messages: de, namespace: "sessions" }) as unknown as ToolGroupT;

function tool(overrides: Partial<ToolEvent> = {}): ToolEvent {
  return {
    kind: "tool",
    uuid: `t-${Math.random()}`,
    ts: "2026-08-17T10:00:00Z",
    name: "Read",
    title: "Read foo.py",
    detail: { file_path: "/foo.py" },
    toolUseId: `tu-${Math.random()}`,
    result: null,
    status: "done",
    stats: null,
    sidechain: false,
    ...overrides,
  };
}

function thinking(overrides: Partial<ThinkingEvent> = {}): ThinkingEvent {
  return {
    kind: "thinking",
    uuid: `th-${Math.random()}`,
    ts: "2026-08-17T10:00:00Z",
    text: "Hmm…",
    sidechain: false,
    ...overrides,
  };
}

describe("summarizeActivity", () => {
  it("counts Bash as commands and everything else as tools", () => {
    const s = summarizeActivity([
      tool({ name: "Bash", title: "npm test" }),
      tool({ name: "Bash", title: "git status" }),
      tool({ name: "Read" }),
    ]);
    expect(s.commands).toBe(2);
    expect(s.tools).toBe(1);
  });

  it("counts repeated thinking blocks", () => {
    expect(summarizeActivity([thinking(), thinking(), thinking()]).thoughts).toBe(3);
  });

  it("aggregates an error from any member of the run", () => {
    expect(summarizeActivity([tool(), tool({ status: "error" })]).hasError).toBe(true);
    expect(summarizeActivity([tool(), tool()]).hasError).toBe(false);
  });

  it("ignores thinking events when deciding whether the run failed", () => {
    expect(summarizeActivity([thinking(), thinking()]).hasError).toBe(false);
  });

  // Operator screenshot 04.09.2026: "84 tools used, 2× thought" with a red
  // warning triangle read as "the whole run broke". It was actually ONE
  // `mc` call that came back 400 and was retried. The line has to say how
  // many failed, or the icon carries an alarm level the number doesn't.
  it("names how many tools failed, separate from the total", () => {
    const s = summarizeActivity([tool(), tool({ status: "error" }), tool(), thinking()]);
    expect(s.tools).toBe(3);
    expect(s.failed).toBe(1);
    expect(s.thoughts).toBe(1);
  });

  it("counts a failed command as failed too", () => {
    const s = summarizeActivity([tool({ name: "Bash", status: "error" })]);
    expect(s.failed).toBe(1);
  });
});

describe("toolGroupLabel", () => {
  it("renders real EN wording, singular and plural, no German", () => {
    const s = summarizeActivity([
      tool({ name: "Bash", title: "npm test" }),
      tool({ name: "Bash", title: "git status" }),
      tool({ name: "Read" }),
    ]);
    const label = toolGroupLabel(s, tEn);
    expect(label).toBe("2 commands executed, 1 tool used");
    expect(label).not.toMatch(/[äöüÄÖÜß]/);
    expect(label).not.toMatch(/ausgeführt|verwendet/);
  });

  it("uses the singular for a single command", () => {
    const s = summarizeActivity([tool({ name: "Bash" })]);
    expect(toolGroupLabel(s, tEn)).toBe("1 command executed");
  });

  it("labels a thinking-only run without a count when there is just one", () => {
    expect(toolGroupLabel(summarizeActivity([thinking()]), tEn)).toBe("Thought");
  });

  it("counts repeated thinking blocks", () => {
    expect(toolGroupLabel(summarizeActivity([thinking(), thinking(), thinking()]), tEn)).toBe("3× thought");
  });

  it("keeps the thinking segment lowercase when it follows another segment", () => {
    const s = summarizeActivity([tool({ name: "Read" }), thinking()]);
    expect(toolGroupLabel(s, tEn)).toBe("1 tool used, thought");
  });

  it("names how many tools failed in the visible line", () => {
    const s = summarizeActivity([tool(), tool({ status: "error" }), tool(), thinking()]);
    expect(toolGroupLabel(s, tEn)).toBe("3 tools used, 1 failed, thought");
  });

  it("pluralises the count, not the invariant word 'failed'", () => {
    const s = summarizeActivity([tool({ status: "error" }), tool({ status: "error" })]);
    expect(toolGroupLabel(s, tEn)).toBe("2 tools used, 2 failed");
  });

  it("falls back to 'Activity' for an empty summary", () => {
    expect(toolGroupLabel(summarizeActivity([]), tEn)).toBe("Activity");
  });

  it("renders the matching German wording in the German UI", () => {
    const s = summarizeActivity([tool({ name: "Bash" }), tool({ name: "Read" }), thinking()]);
    expect(toolGroupLabel(s, tDe)).toBe("1 Befehl ausgeführt, 1 Tool verwendet, nachgedacht");
  });
});

describe("ToolGroup", () => {
  const RUN: ActivityEvent[] = [
    tool({ name: "Bash", title: "npm test" }),
    tool({ name: "Read", title: "Read foo.py" }),
  ];

  it("renders the summary chip and hides the rows until tapped", async () => {
    const user = userEvent.setup();
    render(<ToolGroup events={RUN} detailLevel="normal" />);

    // `useTranslations` is the global test mock (src/test-setup.ts), which
    // does not emulate ICU `{count, plural, …}` — it resolves the key to the
    // raw, unresolved template, which still literally carries both English
    // branch texts ("command executed" / "commands executed"). Matching that
    // substring is enough to prove the wiring reads from `t()`; the exact
    // resolved wording ("1 command executed") is covered by `toolGroupLabel`
    // against the REAL translator above.
    const chip = screen.getByRole("button", { name: /command executed/ });
    expect(chip).toHaveTextContent(/tool used/);
    expect(chip).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Read foo.py")).not.toBeInTheDocument();

    await user.click(chip);
    expect(chip).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Read foo.py")).toBeInTheDocument();
    expect(screen.getByText("npm test")).toBeInTheDocument();
  });

  it("shows the row count next to the label", () => {
    render(<ToolGroup events={RUN} detailLevel="normal" />);
    expect(screen.getByTestId("tool-group")).toHaveTextContent("2");
  });

  it("shows a warning icon when the run contains a failed tool", () => {
    render(<ToolGroup events={[tool(), tool({ status: "error" })]} detailLevel="normal" />);
    expect(screen.getByTestId("tool-group-error-icon")).toBeInTheDocument();
  });

  it("paints a partial failure as a warning, not as the run's failure", () => {
    // Ein fehlgeschlagenes Tool unter vielen ist eine Warnung (Bernstein) —
    // Rot ist dem Lauf vorbehalten, der wirklich gescheitert ist.
    render(<ToolGroup events={[tool(), tool({ status: "error" })]} detailLevel="normal" />);
    expect(screen.getByTestId("tool-group-error-icon").style.color).toBe(STATUS_TEXT.warning);
  });

  it("announces the failure to screen readers, not just in colour", () => {
    // The icon is aria-hidden and the colour is invisible to a screen reader,
    // so without this the group's failure was sighted-only information.
    render(<ToolGroup events={[tool(), tool({ status: "error" })]} detailLevel="normal" />);
    expect(screen.getByRole("button", { name: /error included/ })).toBeInTheDocument();
  });

  it("adds no failure wording to a run that succeeded", () => {
    render(<ToolGroup events={RUN} detailLevel="normal" />);
    expect(screen.queryByRole("button", { name: /error included/ })).not.toBeInTheDocument();
  });

  it("keeps the failure wording out of the visible line", () => {
    render(<ToolGroup events={[tool(), tool({ status: "error" })]} detailLevel="normal" />);
    expect(screen.getByText(/error included/)).toHaveClass("sr-only");
  });

  it("shows the neutral icon when nothing failed", () => {
    render(<ToolGroup events={RUN} detailLevel="normal" />);
    expect(screen.getByTestId("tool-group-icon")).toBeInTheDocument();
    expect(screen.queryByTestId("tool-group-error-icon")).not.toBeInTheDocument();
  });

  it("starts expanded at detailLevel 'verbose'", () => {
    render(<ToolGroup events={RUN} detailLevel="verbose" />);
    expect(screen.getByRole("button", { name: /command executed/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Read foo.py")).toBeInTheDocument();
  });

  it("re-syncs an already-mounted group when the detail level changes", () => {
    const { rerender } = render(<ToolGroup events={RUN} detailLevel="normal" />);
    expect(screen.queryByTestId("tool-group-children")).not.toBeInTheDocument();

    rerender(<ToolGroup events={RUN} detailLevel="verbose" />);
    expect(screen.getByTestId("tool-group-children")).toBeInTheDocument();
  });

  it("renders nothing for an empty run", () => {
    const { container } = render(<ToolGroup events={[]} detailLevel="normal" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("passes the detail level down so child rows open with the group", () => {
    // "Ausführlich" must reach all the way through: the group opens AND
    // ToolRow's own detail block is already rendered — no second click.
    render(<ToolGroup events={RUN} detailLevel="verbose" />);
    // Both rows in the run open, hence getAllByText.
    expect(screen.getAllByText(/file_path/)).toHaveLength(RUN.length);
  });
});

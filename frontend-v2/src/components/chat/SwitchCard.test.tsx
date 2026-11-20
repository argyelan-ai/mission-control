/**
 * The switch card in the timeline (ADR-105, S3 item D): requested, done
 * with a handoff (and its size, shortened n of m), native resume without a
 * handoff, model only, and the red failure card with "Switch back" — plus
 * the routing from ChatMessage and the i18n catalogs.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const apiMock = vi.hoisted(() => ({ switchBack: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      chatRecord: { switchBack: apiMock.switchBack },
      runtimes: { compatMatrix: vi.fn(async () => ({ harnesses: [], host_harnesses: [], runtimes: [] })), listRows: vi.fn(async () => ({ runtimes: [] })) },
    },
  };
});

import { SwitchCardView, formatTokens, offersSwitchBack, switchCardKind } from "./SwitchCard";
import { ChatMessage } from "./ChatMessage";
import { makeLookups } from "./PairPicker";
import { switchEvent } from "@/hooks/useChatSwitch";
import { filterByDetailLevel } from "./detailLevelFilter";
import type { SwitchRow } from "@/lib/api";
import de from "../../../messages/de.json";
import en from "../../../messages/en.json";

const lookups = makeLookups(
  { harnesses: [{ key: "claude", label: "Claude Code" }, { key: "omp", label: "omp" }], host_harnesses: [], runtimes: [] } as never,
  [],
);

function row(over: Partial<SwitchRow> = {}): SwitchRow {
  return {
    eventId: "e1",
    ts: "2026-11-19T10:00:00Z",
    status: "done",
    path: "restart",
    from: { harness: "claude", runtimeId: null, model: "opus" },
    to: { harness: "omp", runtimeId: null, model: "qwen38" },
    package: "# Handoff\nwe renamed a.py",
    estTokens: 6200,
    truncated: false,
    messagesIncluded: 40,
    messagesTotal: 40,
    ...over,
  };
}

const view = (r: SwitchRow, latest = true, onSwitchBack = vi.fn()) =>
  render(<SwitchCardView row={r} latest={latest} lookups={lookups} onSwitchBack={onSwitchBack} />);

describe("switchCardKind", () => {
  it("maps status and path to the five states", () => {
    expect(switchCardKind({ status: "pending", path: "restart" })).toBe("pending");
    expect(switchCardKind({ status: "done", path: "restart" })).toBe("done");
    expect(switchCardKind({ status: "done", path: "restart", nativeResume: true })).toBe("native");
    expect(switchCardKind({ status: "done", path: "model_only" })).toBe("model");
    expect(switchCardKind({ status: "failed", path: "restart" })).toBe("failed");
  });
});

describe("<SwitchCardView>", () => {
  it("pending: 'Switch requested' with both pairs, calm frame, no package yet", () => {
    view(row({ status: "pending", package: null }));
    expect(screen.getByTestId("switch-card").dataset.kind).toBe("pending");
    expect(screen.getByTestId("switch-card-title").textContent).toBe("Switch requested");
    expect(screen.getByTestId("switch-card-pair").textContent).toBe("Claude Code · opus → omp · qwen38");
    expect(screen.queryByTestId("switch-card-package-toggle")).toBeNull();
    expect(screen.queryByTestId("switch-card-back")).toBeNull();
  });

  it("done: the handoff size opens the package text", () => {
    view(row());
    const toggle = screen.getByTestId("switch-card-package-toggle");
    expect(toggle.textContent).toContain("Handoff 6.2k tokens");
    expect(screen.queryByTestId("switch-card-package")).toBeNull();
    fireEvent.click(toggle);
    expect(screen.getByTestId("switch-card-package").textContent).toContain("we renamed a.py");
    expect(screen.queryByTestId("switch-card-truncated")).toBeNull();
  });

  it("done, shortened: 'n of m messages'", () => {
    view(row({ truncated: true, messagesIncluded: 12, messagesTotal: 40 }));
    expect(screen.getByTestId("switch-card-truncated").textContent).toBe("Handoff shortened: 12 of 40 messages");
  });

  it("native resume: says no handoff was used, shows no size", () => {
    view(row({ nativeResume: true }));
    expect(screen.getByTestId("switch-card-native").textContent).toBe("Resumed the old session directly, no handoff");
    expect(screen.queryByTestId("switch-card-package-toggle")).toBeNull();
  });

  it("failed: red card 'still on A', the reason in words, and 'Switch back' on the newest", () => {
    const onBack = vi.fn();
    view(row({ status: "failed", package: null, error: { code: "model_unreachable", detail: "ECONNREFUSED" } }), true, onBack);
    const card = screen.getByTestId("switch-card");
    expect(card.dataset.kind).toBe("failed");
    expect(card.getAttribute("role")).toBe("alert");
    expect(screen.getByTestId("switch-card-title").textContent).toBe("Switch failed — still on Claude Code · opus");
    expect(screen.getByTestId("switch-card-reason").textContent).toBe("To omp · qwen38: The model cannot be reached.");
    fireEvent.click(screen.getByTestId("switch-card-back"));
    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it("an older failed card, or a failed model-only switch, offers no switch back", () => {
    expect(offersSwitchBack({ status: "failed", path: "restart" }, false)).toBe(false);
    expect(offersSwitchBack({ status: "failed", path: "model_only" }, true)).toBe(false);
    expect(offersSwitchBack({ status: "done", path: "restart" }, true)).toBe(false);
    expect(offersSwitchBack({ status: "failed", path: "switch_back" }, true)).toBe(true);
  });

  it("formatTokens follows the UI language", () => {
    expect(formatTokens(950, "en")).toBe("950");
    expect(formatTokens(6200, "en")).toBe("6.2k");
    expect(formatTokens(6200, "de")).toBe("6,2k");
  });
});

describe("ChatMessage routes a switch line to the SwitchCard", () => {
  it("renders the card, not the neutral event row", () => {
    const qc = new QueryClient();
    render(
      <QueryClientProvider client={qc}>
        <ChatMessage ev={switchEvent(row({ status: "pending", package: null }), "chat-1", true)} />
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("switch-card").dataset.kind).toBe("pending");
  });
});

describe("detail levels", () => {
  it("a switch card stays visible at every level, also 'answer only'", () => {
    const ev = switchEvent(row(), "chat-1", true);
    for (const level of ["answer", "compact", "normal", "verbose"] as const) {
      expect(filterByDetailLevel([ev], level)).toContain(ev);
    }
  });
});

describe("i18n — cockpit.switch is complete in both languages", () => {
  function keys(o: unknown, prefix = ""): string[] {
    if (typeof o !== "object" || o === null) return [prefix];
    return Object.entries(o).flatMap(([k, v]) => keys(v, prefix ? `${prefix}.${k}` : k));
  }
  const deKeys = keys((de as { cockpit: { switch: unknown } }).cockpit.switch).sort();
  const enKeys = keys((en as { cockpit: { switch: unknown } }).cockpit.switch).sort();

  it("de and en carry the same keys", () => {
    expect(deKeys).toEqual(enKeys);
    expect(deKeys.length).toBeGreaterThan(30);
  });

  it("every failure code of the switch service has a sentence (backend chat_switch.FAILURE_CODES)", () => {
    for (const code of [
      "process_start_failed", "new_session_failed", "model_unreachable", "provider_error",
      "model_not_applied", "model_not_offered", "orphaned", "superseded",
    ]) {
      expect(deKeys).toContain(`error.${code}`);
    }
  });

  it("every refusal the switch API answers has a sentence", () => {
    for (const code of [
      "pair_incompatible", "no_change", "turn_running", "switch_pending", "default_chat_follows_agent",
      "box_locked", "harness_cannot_hold_chats", "runtime_required", "runtime_not_found", "mode_unsupported",
      "cannot_stop_turn", "nothing_to_switch_back_to", "previous_session_gone", "bad_model",
    ]) {
      expect(enKeys).toContain(`refused.${code}`);
    }
  });
});

/**
 * The pair picker (ADR-105, S3 item D): which pairs it offers (from the
 * compat matrix, never a local list), what "loaded" and "needs the box"
 * mean, and the busy state machine — the two-button sheet shows exactly
 * the options the server's 409 named, "after this answer" only when offered.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const apiMock = vi.hoisted(() => ({
  switchChat: vi.fn(),
  switchBack: vi.fn(),
  compatMatrix: vi.fn(),
  listRows: vi.fn(),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      chatRecord: { switchChat: apiMock.switchChat, switchBack: apiMock.switchBack },
      runtimes: { compatMatrix: apiMock.compatMatrix, listRows: apiMock.listRows },
    },
  };
});

import {
  CHAT_HARNESSES,
  PairPicker,
  buildPairOptions,
  makeLookups,
  pairLabel,
  switchActionReducer,
  type SwitchActionState,
} from "./PairPicker";
import { switchRefusal, type RecordChat } from "@/lib/api";
import type { CompatMatrix, Runtime } from "@/lib/types";

const BOX_A = { id: "h-a", slug: "box-a", display_name: "Box A" };

function rt(over: Partial<Runtime> & { id: string; slug: string }): Runtime {
  return {
    display_name: over.slug,
    runtime_type: "vllm",
    provider: "local",
    endpoint: "http://box.invalid",
    healthcheck_path: "/health",
    container_name: null,
    role_tags: [],
    supports_tools: true,
    supports_reasoning: false,
    supports_streaming: true,
    preferred_context_len: 0,
    max_context_len: 0,
    gpu_profile: "",
    memory_notes: "",
    startup_notes: "",
    ui_order: 0,
    enabled: true,
    ...over,
  } as Runtime;
}

const RUNTIMES: Runtime[] = [
  rt({ id: "rt-opus", slug: "anthropic-claude-opus", model_identifier: "claude-opus", locality: "cloud" }),
  rt({ id: "rt-qwen", slug: "qwen-box-a", model_identifier: "qwen38", locality: "local", host: BOX_A, serving_since: "2026-11-19T08:00:00Z" }),
  rt({ id: "rt-glm", slug: "glm-box-a", model_identifier: "glm53", locality: "local", host: BOX_A, serving_since: null }),
  rt({ id: "rt-off", slug: "off", model_identifier: "off", locality: "local", enabled: false }),
];

const MATRIX: CompatMatrix = {
  harnesses: [
    { key: "claude", label: "Claude Code" },
    { key: "omp", label: "omp" },
    { key: "codex", label: "Codex" },
  ] as CompatMatrix["harnesses"],
  host_harnesses: [],
  runtimes: [
    { slug: "anthropic-claude-opus", display_name: "Opus", protocol: "anthropic", compatible_harnesses: ["claude"], reasons: {} },
    { slug: "qwen-box-a", display_name: "Qwen", protocol: "openai", compatible_harnesses: ["omp", "codex"], reasons: {} },
    { slug: "glm-box-a", display_name: "GLM", protocol: "openai", compatible_harnesses: ["omp", "codex"], reasons: {} },
    { slug: "off", display_name: "Off", protocol: "openai", compatible_harnesses: ["omp"], reasons: {} },
  ] as CompatMatrix["runtimes"],
};

function chat(over: Partial<RecordChat> = {}): RecordChat {
  return {
    id: "chat-1",
    title: "Demo",
    harness: "claude",
    model: "claude-opus",
    legacyAgentId: "agent-1",
    agentDefault: false,
    nativeSessionId: null,
    pinned: false,
    archived: false,
    lastActivityAt: null,
    createdAt: null,
    runtimeId: "rt-opus",
    ...over,
  };
}

describe("buildPairOptions", () => {
  const others = [
    chat({ id: "c2", runtimeId: "rt-qwen" }),
    chat({ id: "c3", runtimeId: "rt-qwen" }),
    chat({ id: "c4", runtimeId: "rt-qwen", archived: true }),
  ];
  const opts = buildPairOptions({ matrix: MATRIX, rows: RUNTIMES, chat: chat(), chats: [chat(), ...others] });

  it("lists only harnesses that can hold a chat, and only compatible runtimes", () => {
    expect([...opts.keys()]).toEqual(["claude", "omp"]);
    expect(CHAT_HARNESSES).not.toContain("codex");
    expect(opts.get("claude")!.map((o) => o.runtimeId)).toEqual(["rt-opus"]);
    // A disabled runtime is not offered.
    expect(opts.get("omp")!.map((o) => o.runtimeId)).toEqual(["rt-qwen", "rt-glm"]);
  });

  it("marks the current pair, the loaded local model and the box switch with its cost", () => {
    expect(opts.get("claude")![0]).toMatchObject({ current: true, local: false });
    const [qwen, glm] = opts.get("omp")!;
    expect(qwen).toMatchObject({ local: true, loaded: true, affects: 0, box: "Box A" });
    // GLM is not up: Box A must switch, which takes it from the 2 open chats on Qwen.
    expect(glm).toMatchObject({ local: true, loaded: false, affects: 2 });
  });

  it("returns nothing while matrix or rows are loading", () => {
    expect(buildPairOptions({ matrix: undefined, rows: RUNTIMES, chat: chat(), chats: [] }).size).toBe(0);
  });
});

describe("pairLabel", () => {
  const lookups = makeLookups(MATRIX, RUNTIMES);
  it("harness label, then model; the runtime's model when the pair has none", () => {
    expect(pairLabel({ harness: "claude", runtimeId: "rt-opus", model: "opus" }, lookups)).toBe("Claude Code · opus");
    expect(pairLabel({ harness: "omp", runtimeId: "rt-qwen", model: null }, lookups)).toBe("omp · qwen38");
    expect(pairLabel(null, lookups)).toBe("");
  });
});

describe("switchActionReducer — the busy sheet comes from the server's options", () => {
  const sending: SwitchActionState = {
    phase: "sending",
    target: { kind: "pair", body: { harness: "omp", runtimeId: "rt-qwen" } },
  };
  const refusal = (options: string[], code = "turn_running") => ({ status: 409, code, options, message: null });

  it("409 turn_running with stop_and_switch → busy with that one option (no after_turn)", () => {
    const s = switchActionReducer(sending, { type: "refused", refusal: refusal(["stop_and_switch"]) });
    expect(s).toMatchObject({ phase: "busy", options: ["stop_and_switch"] });
  });

  it("after_turn appears only when the server offers it; unknown modes never", () => {
    const s = switchActionReducer(sending, {
      type: "refused",
      refusal: refusal(["stop_and_switch", "after_turn", "teleport"]),
    });
    expect(s).toMatchObject({ phase: "busy", options: ["stop_and_switch", "after_turn"] });
  });

  it("no option the UI knows, or a mode already chosen → a refusal, not a loop", () => {
    expect(switchActionReducer(sending, { type: "refused", refusal: refusal([]) })).toMatchObject({ phase: "refused" });
    const chosen: SwitchActionState = {
      phase: "sending",
      target: { kind: "pair", body: { harness: "omp", runtimeId: "rt-qwen", mode: "stop_and_switch" } },
    };
    expect(switchActionReducer(chosen, { type: "refused", refusal: refusal(["stop_and_switch"]) })).toMatchObject({
      phase: "refused",
      code: "turn_running",
    });
  });

  it("other refusals keep their code", () => {
    const s = switchActionReducer(sending, { type: "refused", refusal: refusal([], "box_locked") });
    expect(s).toMatchObject({ phase: "refused", code: "box_locked" });
  });
});

describe("switchRefusal", () => {
  it("reads code and options from request()'s error text", () => {
    const err = new Error(
      'API 409: {"detail":{"reason":"turn_running","code":"turn_running","options":["stop_and_switch"],"state":"running"}}',
    );
    expect(switchRefusal(err)).toEqual({ status: 409, code: "turn_running", options: ["stop_and_switch"], message: null });
    expect(switchRefusal(new Error("boom"))).toBeNull();
  });
});

describe("<PairPicker>", () => {
  beforeEach(() => {
    apiMock.compatMatrix.mockResolvedValue(MATRIX);
    apiMock.listRows.mockResolvedValue({ runtimes: RUNTIMES });
    apiMock.switchChat.mockReset();
  });

  function mount(c: RecordChat) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const onClose = vi.fn();
    render(
      <QueryClientProvider client={qc}>
        <PairPicker open onClose={onClose} chat={c} chats={[c]} agentId="agent-1" />
      </QueryClientProvider>,
    );
    return { onClose };
  }

  it("the agent's main chat says it follows the agent and lists no pairs", async () => {
    mount(chat({ agentDefault: true }));
    expect(await screen.findByTestId("pair-follows-agent")).toBeTruthy();
    expect(screen.queryAllByTestId("pair-option")).toHaveLength(0);
  });

  it("busy: 409 opens the sheet with only 'Stop and switch'; tapping it resends with that mode", async () => {
    apiMock.switchChat
      .mockRejectedValueOnce(
        new Error('API 409: {"detail":{"code":"turn_running","reason":"turn_running","options":["stop_and_switch"]}}'),
      )
      .mockResolvedValueOnce({ eventId: "e1", status: "pending", path: "restart", ts: "2026-11-19T10:00:00Z" });
    const { onClose } = mount(chat());
    fireEvent.click(await screen.findByTestId("pair-harness-omp"));
    const qwen = (await screen.findAllByTestId("pair-option")).find((b) => b.dataset.pair === "omp:rt-qwen")!;
    fireEvent.click(qwen);
    expect(await screen.findByTestId("switch-busy-sheet")).toBeTruthy();
    expect(screen.getByTestId("switch-busy-stop_and_switch").textContent).toBe("Stop and switch");
    expect(screen.queryByTestId("switch-busy-after_turn")).toBeNull();
    fireEvent.click(screen.getByTestId("switch-busy-stop_and_switch"));
    await waitFor(() => expect(apiMock.switchChat).toHaveBeenCalledTimes(2));
    expect(apiMock.switchChat).toHaveBeenLastCalledWith("chat-1", {
      harness: "omp",
      runtimeId: "rt-qwen",
      model: "qwen38",
      mode: "stop_and_switch",
    });
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("a box-lock refusal stays in the picker as a sentence", async () => {
    apiMock.switchChat.mockRejectedValueOnce(new Error('API 423: {"detail":{"code":"box_locked","reason":"box_locked"}}'));
    mount(chat());
    fireEvent.click(await screen.findByTestId("pair-harness-omp"));
    const glm = (await screen.findAllByTestId("pair-option")).find((b) => b.dataset.pair === "omp:rt-glm")!;
    expect(glm.textContent).toBe("glm53Box A");
    fireEvent.click(glm);
    expect((await screen.findByTestId("pair-refused")).textContent).toBe("The box is switching right now. Try again shortly.");
  });
});

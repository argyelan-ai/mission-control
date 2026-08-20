/**
 * ChatView — Task B6 vitest (revised: Terminal moved from a side panel to a
 * center-view toggle in ChatView's own header, next to the detail switcher).
 *
 * Coverage: the Chat/Terminal header toggle switches the center content,
 * no-transcript agents (and the belt-and-braces runtime-404 case) force
 * terminal mode and disable the Chat segment, detail-level filtering
 * (Kompakt/Normal/Ausführlich), the approval card only on a permission
 * prompt, and outbound actions (send / stop / answer) reaching
 * `api.chat.*` with the right arguments.
 *
 * `useChatStream` and `TerminalPanel` are both mocked — the stream's own
 * reducer/hook wiring is covered by useChatStream.test.ts, and
 * TerminalPanel's xterm/WebSocket machinery is out of scope here (it's a
 * verbatim move covered by its own future test surface / manual live-gate).
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  ChatView,
  buildTimelineItems,
  modelBadgeUuids,
  ACTIVITY_GROUP_MIN_SIZE,
  headerSideReservation,
  MOBILE_HEADER_METRICS,
} from "./ChatView";
import { useChatStream, type UseChatStreamResult } from "@/hooks/useChatStream";
import { api } from "@/lib/api";
import type { AgentWithState } from "./TerminalPanel";
import type { MessageEvent, ThinkingEvent, ToolEvent } from "@/lib/chatTypes";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// jsdom rechnet kein Layout — eine Trefferflaeche laesst sich hier nur ueber
// die Klassen UND die dahinterliegende CSS-Regel festhalten. Darum lesen die
// Kopfzeilen-Tests globals.css als Text mit.
// (Nicht `new URL(..., import.meta.url)`: genau diese Form schreibt Vite in
// eine Asset-URL um, die dann kein file:-Pfad mehr ist.)
const GLOBALS_CSS = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "../../styles/globals.css"),
  "utf-8"
);

/** Der sichtbare Kreis IM Knopf (der Knopf selbst ist die Trefferflaeche). */
function circleOf(label: string): HTMLElement {
  const el = screen.getByLabelText(label).firstElementChild;
  if (!(el instanceof HTMLElement)) throw new Error(`kein Kreis in "${label}"`);
  return el;
}

vi.mock("@/hooks/useChatStream", () => ({ useChatStream: vi.fn() }));
vi.mock("@/lib/api", () => ({
  api: {
    chat: {
      sendText: vi.fn().mockResolvedValue(undefined),
      sendKeys: vi.fn().mockResolvedValue(undefined),
      // The Composer rendered inside ChatView reaches for this one directly
      // (effort switching); stubbed so the mock stays a faithful stand-in even
      // though no test here drives the chip.
      setEffort: vi.fn().mockResolvedValue(undefined),
    },
  },
}));
// Die echte VoiceButton haengt an <VoiceProvider>, und der baut beim Mounten
// einen LiveKit-Room auf — fuer einen Kopfzeilen-Test viel zu schwer. Der Stub
// haelt genau das fest, was ChatView zu verantworten hat: dass auf dem Handy
// ueberhaupt ein Zugang zur Sprachbedienung im Kopf steht.
vi.mock("@/components/voice/VoiceWidget", () => ({
  VoiceButton: () => (
    <button type="button" aria-label="Start voice assistant">
      mic
    </button>
  ),
}));
vi.mock("./TerminalPanel", async () => {
  const actual = await vi.importActual<typeof import("./TerminalPanel")>("./TerminalPanel");
  return {
    ...actual,
    TerminalPanel: ({ agent }: { agent: { name: string } }) => (
      <div data-testid="terminal-panel-stub">Terminal-Panel: {agent.name}</div>
    ),
  };
});

// cmdk's <Command.List> (inside Composer) reaches for ResizeObserver and
// scrollIntoView — neither exists in jsdom (same stub as Composer.test.tsx).
beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
  class MockResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  window.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
});

const mockUseChatStream = vi.mocked(useChatStream);

function mkAgent(overrides: Partial<AgentWithState> = {}): AgentWithState {
  return {
    id: "agent-1",
    board_id: null,
    name: "Cody",
    role: null,
    emoji: null,
    status: "idle",
    model: null,
    secret_id: null,
    is_board_lead: false,
    heartbeat_config: { interval: "5m", target: "boss" },
    skills: [],
    skill_filter: null,
    cli_plugins: null,
    cli_skills: null,
    mcp_servers: null,
    scopes: [],
    identity_md: null,
    soul_md: null,
    tools_md: null,
    heartbeat_md: null,
    rules_md: null,
    memory_md: null,
    last_seen_at: null,
    last_task_activity_at: null,
    current_task_id: null,
    context_tokens: 0,
    context_max: 200000,
    session_message_count: 0,
    total_tasks_completed: 0,
    total_compactions: 0,
    template_id: null,
    workspace_path: null,
    provision_status: "local",
    provisioned_at: null,
    archived_at: null,
    discord_channel_id: null,
    discord_channel_name: null,
    last_trigger_at: null,
    last_dispatch_error: null,
    run_state: "idle",
    operational_mode: "active",
    agent_runtime: "cli-bridge",
    runtime_id: null,
    pending_runtime_sync: false,
    harness: null,
    runtime_switchable: false,
    runtime_switch_blocked_reason: null,
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    container_state: "running",
    ...overrides,
  };
}

function mkStream(overrides: Partial<UseChatStreamResult> = {}): UseChatStreamResult {
  return {
    events: [],
    state: null,
    usage: null,
    session: { sessionId: "s1", live: true, startedAt: "2026-08-15T10:00:00Z" },
    hasMore: false,
    connected: true,
    loading: false,
    error: null,
    capabilities: null,
    pendingEchoes: [],
    echoSent: vi.fn(),
    echoFailed: vi.fn(),
    echoAgentStarting: vi.fn(),
    awaitingResponse: false,
    ...overrides,
  };
}

const MSG: MessageEvent = {
  kind: "message",
  uuid: "u1",
  ts: "2026-08-15T10:00:00Z",
  role: "assistant",
  text: "Hallo!",
  model: "claude-sonnet-4-6",
  sidechain: false,
};

const TOOL: ToolEvent = {
  kind: "tool",
  uuid: "u2",
  ts: "2026-08-15T10:00:01Z",
  name: "Read",
  title: "Read foo.py",
  detail: { file_path: "/foo.py" },
  toolUseId: "tu-1",
  result: null,
  status: "done",
  stats: null,
  sidechain: false,
};

const THINKING: ThinkingEvent = {
  kind: "thinking",
  uuid: "u3",
  ts: "2026-08-15T10:00:02Z",
  text: "Hmm, let me check...",
  sidechain: false,
};

const noop = () => {};

// ── Grouping logic ─────────────────────────────────────────────────────────
// Pure function, tested directly: the render tests below only need to prove
// the wiring, not re-derive every boundary case through the DOM.

function mkTool(overrides: Partial<ToolEvent> = {}): ToolEvent {
  return { ...TOOL, uuid: `t-${Math.random()}`, toolUseId: `tu-${Math.random()}`, ...overrides };
}

function mkThinking(overrides: Partial<ThinkingEvent> = {}): ThinkingEvent {
  return { ...THINKING, uuid: `th-${Math.random()}`, ...overrides };
}

function mkMsg(overrides: Partial<MessageEvent> = {}): MessageEvent {
  return { ...MSG, uuid: `m-${Math.random()}`, ...overrides };
}

describe("buildTimelineItems", () => {
  it("collapses consecutive tool/thinking events into one activity run", () => {
    const items = buildTimelineItems([mkTool(), mkThinking(), mkTool()]);
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: "activity" });
    expect(items[0].kind === "activity" && items[0].events).toHaveLength(3);
  });

  it("ends a run at an assistant message", () => {
    const items = buildTimelineItems([mkTool(), mkTool(), mkMsg(), mkTool(), mkTool()]);
    expect(items.map((i) => i.kind)).toEqual(["activity", "single", "activity"]);
  });

  it("ends a run at a user message", () => {
    const items = buildTimelineItems([mkTool(), mkTool(), mkMsg({ role: "user", text: "Weiter" }), mkTool(), mkTool()]);
    expect(items.map((i) => i.kind)).toEqual(["activity", "single", "activity"]);
  });

  it("ends a run at a slash command", () => {
    const cmd = { kind: "command", uuid: "c1", ts: MSG.ts, command: "/clear" } as const;
    const items = buildTimelineItems([mkTool(), mkTool(), cmd, mkTool(), mkTool()]);
    expect(items.map((i) => i.kind)).toEqual(["activity", "single", "activity"]);
  });

  it(`emits runs shorter than ${ACTIVITY_GROUP_MIN_SIZE} as plain rows`, () => {
    const items = buildTimelineItems([mkMsg(), mkTool(), mkMsg()]);
    expect(items.map((i) => i.kind)).toEqual(["single", "single", "single"]);
  });

  it("keeps sidechain runs separate from top-level activity runs", () => {
    const items = buildTimelineItems([
      mkTool(),
      mkTool(),
      mkTool({ sidechain: true }),
      mkTool({ sidechain: true }),
      mkTool(),
      mkTool(),
    ]);
    expect(items.map((i) => i.kind)).toEqual(["activity", "sidechain", "activity"]);
  });

  it("keeps a single sidechain event grouped (SubagentGroup owns its own header)", () => {
    const items = buildTimelineItems([mkTool({ sidechain: true })]);
    expect(items.map((i) => i.kind)).toEqual(["sidechain"]);
  });

  it("preserves event order across mixed input", () => {
    const first = mkMsg({ text: "A" });
    const last = mkMsg({ text: "B" });
    const items = buildTimelineItems([first, mkTool(), mkTool(), last]);
    expect(items[0]).toMatchObject({ kind: "single", event: first });
    expect(items[2]).toMatchObject({ kind: "single", event: last });
  });

  it("returns nothing for an empty timeline", () => {
    expect(buildTimelineItems([])).toEqual([]);
  });
});

describe("modelBadgeUuids", () => {
  it("flags the first assistant message so the reader knows what is answering", () => {
    const a = mkMsg({ model: "sonnet" });
    expect(modelBadgeUuids([a])).toEqual(new Set([a.uuid]));
  });

  it("does not repeat the model on every turn", () => {
    const a = mkMsg({ model: "sonnet" });
    const b = mkMsg({ model: "sonnet" });
    expect(modelBadgeUuids([a, b])).toEqual(new Set([a.uuid]));
  });

  it("flags the turn where the model actually changed", () => {
    const a = mkMsg({ model: "sonnet" });
    const b = mkMsg({ model: "sonnet" });
    const c = mkMsg({ model: "opus" });
    expect(modelBadgeUuids([a, b, c])).toEqual(new Set([a.uuid, c.uuid]));
  });

  it("ignores user messages and events without a model", () => {
    const user = mkMsg({ role: "user", model: null, text: "Wechsle das Modell" });
    const a = mkMsg({ model: null });
    expect(modelBadgeUuids([user, a, mkTool()])).toEqual(new Set());
  });

  it("renders the model line only on the changed turn", () => {
    mockUseChatStream.mockReturnValue(
      mkStream({
        events: [
          { ...MSG, uuid: "m1", text: "Erste", model: "sonnet" },
          { ...MSG, uuid: "m2", text: "Zweite", model: "sonnet" },
          { ...MSG, uuid: "m3", text: "Dritte", model: "opus" },
        ],
      })
    );
    renderChatView();

    expect(screen.getByText("sonnet")).toBeInTheDocument();
    expect(screen.getByText("opus")).toBeInTheDocument();
    // Once each, not once per message.
    expect(screen.getAllByText("sonnet")).toHaveLength(1);
  });
});

function renderChatView(overrides: Partial<React.ComponentProps<typeof ChatView>> = {}) {
  return render(
    <ChatView
      agent={mkAgent()}
      hasTranscript
      detailLevel="normal"
      onDetailLevelChange={noop}
      centerView="chat"
      onCenterViewChange={noop}
      {...overrides}
    />
  );
}

describe("ChatView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the chat timeline in chat mode", () => {
    mockUseChatStream.mockReturnValue(mkStream({ events: [MSG] }));
    renderChatView();
    expect(screen.getByText("Hallo!")).toBeInTheDocument();
    expect(screen.queryByTestId("terminal-panel-stub")).not.toBeInTheDocument();
  });

  it("switching the header toggle to Terminal swaps the center content", async () => {
    mockUseChatStream.mockReturnValue(mkStream({ events: [MSG] }));
    const onCenterViewChange = vi.fn();
    const user = userEvent.setup();
    renderChatView({ onCenterViewChange });

    await user.click(screen.getByRole("button", { name: "Terminal" }));
    expect(onCenterViewChange).toHaveBeenCalledWith("terminal");
  });

  it("centerView='terminal' renders TerminalPanel instead of the timeline/composer", () => {
    mockUseChatStream.mockReturnValue(mkStream({ events: [MSG] }));
    renderChatView({ centerView: "terminal" });

    expect(screen.getByTestId("terminal-panel-stub")).toHaveTextContent("Terminal-Panel: Cody");
    expect(screen.queryByText("Hallo!")).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Message the agent…")).not.toBeInTheDocument();
  });

  it("switching back to Chat calls onCenterViewChange('chat')", async () => {
    mockUseChatStream.mockReturnValue(mkStream());
    const onCenterViewChange = vi.fn();
    const user = userEvent.setup();
    renderChatView({ centerView: "terminal", onCenterViewChange });

    await user.click(screen.getByRole("button", { name: "Chat" }));
    expect(onCenterViewChange).toHaveBeenCalledWith("chat");
  });

  it("hides the detail-level switcher while in terminal mode", () => {
    mockUseChatStream.mockReturnValue(mkStream());
    renderChatView({ centerView: "terminal" });
    expect(screen.queryByRole("button", { name: "Compact" })).not.toBeInTheDocument();
  });

  it("shows the detail-level switcher in chat mode", () => {
    // Seit 19.08.2026 ist der Umschalter EIN Knopf mit Klappliste statt drei
    // Segmenten (Operator-Wunsch, Kopfzeile entlasten). Die Stufen sind erst
    // nach dem Klick da — die Abdeckung dafuer steht in
    // "Desktop-Kopfzeile — Detailgrad eingeklappt".
    mockUseChatStream.mockReturnValue(mkStream());
    renderChatView({ centerView: "chat" });
    expect(screen.getByTestId("detail-level-trigger")).toBeInTheDocument();
  });

  it("no-transcript agents force terminal mode and disable the Chat segment", () => {
    mockUseChatStream.mockReturnValue(mkStream());
    renderChatView({ hasTranscript: false, centerView: "chat" });

    expect(screen.getByTestId("terminal-panel-stub")).toBeInTheDocument();
    const chatButton = screen.getByRole("button", { name: "Chat" });
    expect(chatButton).toBeDisabled();
    expect(screen.getByRole("button", { name: "Terminal" })).toHaveAttribute("aria-pressed", "true");
  });

  it("a runtime no_transcript 404 also forces terminal mode, even if hasTranscript was true", () => {
    mockUseChatStream.mockReturnValue(
      mkStream({ error: new Error('API 404: {"reason":"no_transcript"}') })
    );
    renderChatView({ hasTranscript: true, centerView: "chat" });

    expect(screen.getByTestId("terminal-panel-stub")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Chat" })).toBeDisabled();
  });

  it("does not fetch the stream at all when hasTranscript is false", () => {
    mockUseChatStream.mockReturnValue(mkStream());
    renderChatView({ hasTranscript: false });
    expect(mockUseChatStream).toHaveBeenCalledWith("agent-1", false);
  });

  it("Kompakt hides tool and thinking rows entirely", () => {
    mockUseChatStream.mockReturnValue(mkStream({ events: [MSG, TOOL, THINKING] }));
    renderChatView({ detailLevel: "compact" });

    expect(screen.getByText("Hallo!")).toBeInTheDocument();
    expect(screen.queryByText("Read foo.py")).not.toBeInTheDocument();
    expect(screen.queryByText("Denkt nach…")).not.toBeInTheDocument();
    // No group chip either — Kompakt hides the activity entirely, it doesn't
    // trade a wall of rows for a wall of chips.
    expect(screen.queryByTestId("tool-group")).not.toBeInTheDocument();
  });

  it("Normal collapses a run of tool/thinking events into one group chip", async () => {
    mockUseChatStream.mockReturnValue(mkStream({ events: [MSG, TOOL, THINKING] }));
    const user = userEvent.setup();
    renderChatView({ detailLevel: "normal" });

    // The wall of rows is gone by default — one summary chip stands in for it.
    const chip = screen.getByRole("button", { name: /1 Tool verwendet, nachgedacht/ });
    expect(screen.queryByText("Read foo.py")).not.toBeInTheDocument();
    expect(screen.queryByText("Denkt nach…")).not.toBeInTheDocument();

    await user.click(chip);
    expect(screen.getByText("Read foo.py")).toBeInTheDocument();
    expect(screen.getByText("Denkt nach…")).toBeInTheDocument();
    // Rows themselves are still collapsed at Normal — that's Ausführlich's job.
    expect(screen.queryByText(/file_path/)).not.toBeInTheDocument();
  });

  it("Normal leaves a lone tool event as a plain row (no group chip)", () => {
    mockUseChatStream.mockReturnValue(mkStream({ events: [MSG, TOOL, { ...MSG, uuid: "u9", text: "Fertig." }] }));
    renderChatView({ detailLevel: "normal" });

    expect(screen.getByText("Read foo.py")).toBeInTheDocument();
    expect(screen.queryByTestId("tool-group")).not.toBeInTheDocument();
  });

  it("Ausführlich expands tool/thinking rows by default", () => {
    mockUseChatStream.mockReturnValue(mkStream({ events: [MSG, TOOL, THINKING] }));
    renderChatView({ detailLevel: "verbose" });

    expect(screen.getByText(/file_path/)).toBeInTheDocument();
    expect(screen.getByText("Hmm, let me check...")).toBeInTheDocument();
  });

  it("clicking a detail-level button calls onDetailLevelChange", async () => {
    mockUseChatStream.mockReturnValue(mkStream({ events: [MSG] }));
    const onChange = vi.fn();
    const user = userEvent.setup();
    renderChatView({ onDetailLevelChange: onChange });

    await user.click(screen.getByTestId("detail-level-trigger"));
    await user.click(screen.getByRole("option", { name: "Verbose" }));
    expect(onChange).toHaveBeenCalledWith("verbose");
  });

  it("shows the ApprovalCard only when state.status is permission_prompt", () => {
    mockUseChatStream.mockReturnValue(
      mkStream({
        state: {
          kind: "state",
          status: "permission_prompt",
          prompt: { question: "Datei löschen?", options: [{ key: "y", label: "Ja" }] },
        },
      })
    );
    renderChatView();

    expect(screen.getByText("Datei löschen?")).toBeInTheDocument();
  });

  it("the ApprovalCard's terminal escape hatch calls onCenterViewChange('terminal')", async () => {
    mockUseChatStream.mockReturnValue(
      mkStream({
        state: {
          kind: "state",
          status: "permission_prompt",
          prompt: { question: "Löschen?", options: [{ key: "1", label: "Ja" }] },
        },
      })
    );
    const onCenterViewChange = vi.fn();
    const user = userEvent.setup();
    renderChatView({ onCenterViewChange });

    await user.click(screen.getByText("Im Terminal prüfen"));
    expect(onCenterViewChange).toHaveBeenCalledWith("terminal");
  });

  it("answering the approval sends the bare key via api.chat.sendKeys (no Enter)", async () => {
    mockUseChatStream.mockReturnValue(
      mkStream({
        state: {
          kind: "state",
          status: "permission_prompt",
          prompt: { question: "Löschen?", options: [{ key: "1", label: "Ja" }] },
        },
      })
    );
    const user = userEvent.setup();
    renderChatView();

    await user.click(screen.getByRole("button", { name: "Ja" }));
    expect(api.chat.sendKeys).toHaveBeenCalledWith("agent-1", ["1"]);
    expect(api.chat.sendKeys).toHaveBeenCalledTimes(1);
  });

  it("sending a composer message calls api.chat.sendText", async () => {
    mockUseChatStream.mockReturnValue(mkStream());
    const user = userEvent.setup();
    renderChatView();

    await user.type(screen.getByPlaceholderText("Message the agent…"), "Hi");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(api.chat.sendText).toHaveBeenCalledWith("agent-1", "Hi");
  });

  it("stopping sends Escape via api.chat.sendKeys", async () => {
    mockUseChatStream.mockReturnValue(mkStream({ state: { kind: "state", status: "working", prompt: null } }));
    const user = userEvent.setup();
    renderChatView();

    await user.click(screen.getByRole("button", { name: "Stop" }));
    expect(api.chat.sendKeys).toHaveBeenCalledWith("agent-1", ["Escape"]);
  });

  // Regression (Operator-Befund 20.08.2026): "wenn man den Chat wieder
  // oeffnet, beginnt er ganz am Anfang des Gespraechs".
  //
  // Ursache war ein Wettlauf: Erst rendern nur die letzten 30 Eintraege, einen
  // Frame spaeter mounten ALLE — oberhalb des Sichtfensters. Die Hoehe springt,
  // `scrollTop` bleibt stehen, der Browser feuert dafuer ein Scroll-Ereignis,
  // und `handleScroll` deutete das als "der Nutzer hat hochgescrollt" und
  // schaltete das Mitlaufen ab. Danach sprang nichts mehr ans Ende.
  //
  // Die Regel lautet jetzt: Mitlaufen wird NUR durch eine echte Geste beendet.
  // Hilfsmittel: die ResizeObserver-Rueckmeldung ist der reale "naechste
  // Anlass", bei dem die Ansicht wieder ans Ende springt, wenn sie mitlaeuft.
  function mitBeobachter(
    fn: (el: HTMLElement, ausloesen: () => void, setzeHoehe: (h: number) => void) => void
  ) {
    const original = window.ResizeObserver;
    const observed: Element[] = [];
    let fire: (() => void) | null = null;
    class Capturing {
      constructor(cb: ResizeObserverCallback) {
        fire = () => cb([], this as unknown as ResizeObserver);
      }
      observe(el: Element) { observed.push(el); }
      unobserve() {}
      disconnect() {}
    }
    window.ResizeObserver = Capturing as unknown as typeof ResizeObserver;
    try {
      mockUseChatStream.mockReturnValue(mkStream({ events: [MSG] }));
      renderChatView();
      const el = observed[0] as HTMLElement;
      Object.defineProperty(el, "clientHeight", { value: 600, configurable: true });
      Object.defineProperty(el, "scrollHeight", { value: 6000, configurable: true });
      fn(el, () => fire!(), (h: number) =>
        Object.defineProperty(el, "scrollHeight", { value: h, configurable: true })
      );
    } finally {
      window.ResizeObserver = original;
    }
  }

  it("bleibt am Ende, wenn der Inhalt oberhalb waechst (kein Nutzer-Scroll)", () => {
    mitBeobachter((el, ausloesen, setzeHoehe) => {
      // Genau der Umbruch — und zwar so, wie er im Browser wirklich aussieht:
      // die HOEHE springt (hier 950 -> 6000), `scrollTop` bleibt stehen, der
      // Browser feuert dafuer ein Scroll-Ereignis. OHNE Geste.
      //
      // Frueher stand hier stattdessen `el.scrollTop = 350` bei
      // gleichbleibender Hoehe. Das ist aber das Bild einer verschobenen
      // ANSICHT (Seitensuche, scrollIntoView), nicht das eines Layout-Umbruchs
      // — die Nachstellung traf den Fehler also gar nicht.
      setzeHoehe(950);
      el.scrollTop = 350; // am Ende: 950 - 350 - 600 = 0
      fireEvent.scroll(el);

      setzeHoehe(6000);
      fireEvent.scroll(el);

      ausloesen();
      expect(el.scrollTop).toBe(6000);
    });
  });

  // ── Loecher im ersten Anlauf (Review 20.08.2026) ───────────────────────────

  it("ein Antippen im Verlauf ist keine Scroll-Absicht", () => {
    mitBeobachter((el, ausloesen, setzeHoehe) => {
      // Nachricht antippen zum Kopieren, "Mehr anzeigen", Fehlgriff: der erste
      // Anlauf bewaffnete die Geste schon beim blossen Aufsetzen des Fingers
      // (`onPointerDown`) und raeumte sie nur im `atBottom`-Zweig wieder ab.
      // Einmal bewaffnet, galt der naechste layoutbedingte Scroll (spaetes
      // Markdown-/Bild-/Schrift-Reflow) wieder als Geste — also genau der
      // Fehler, den der Fix beheben sollte.
      setzeHoehe(950);
      el.scrollTop = 350;
      fireEvent.scroll(el);

      fireEvent.pointerDown(el, { buttons: 1 });
      fireEvent.pointerUp(el);

      setzeHoehe(6000); // spaetes Reflow
      fireEvent.scroll(el);

      ausloesen();
      expect(el.scrollTop).toBe(6000);
    });
  });

  it("ein Ziehen im Verlauf ist eine Scroll-Absicht", () => {
    mitBeobachter((el, ausloesen) => {
      // Die Rollbalken-Geste mit der Maus erzeugt weder `wheel` noch
      // `touchmove` — nur gedrueckte Zeigerbewegung. Die muss weiterhin zaehlen.
      fireEvent.pointerDown(el, { buttons: 1 });
      fireEvent.pointerMove(el, { buttons: 1 });
      el.scrollTop = 350;
      fireEvent.scroll(el);

      ausloesen();
      expect(el.scrollTop).toBe(350);
    });
  });

  it("eine verschobene Ansicht ohne Geste beendet das Mitlaufen auch", () => {
    mitBeobachter((el, ausloesen) => {
      // Spiegelfall: Seitensuche im Browser, `scrollIntoView`, wiederhergestellte
      // Scroll-Position. Kein Eingabegeraet meldet sich, die Hoehe bleibt
      // gleich — trotzdem steht die Ansicht jetzt woanders. Wer sie beim
      // naechsten Ereignis nach unten reisst, hat den Text weggezogen, den der
      // Operator gerade liest.
      el.scrollTop = 6000 - 600; // am Ende, Mitlaufen an
      fireEvent.scroll(el);

      el.scrollTop = 350; // Sprung nach oben, Hoehe UNVERAENDERT
      fireEvent.scroll(el);

      ausloesen();
      expect(el.scrollTop).toBe(350);
    });
  });

  it("eine Geste ueberlebt den Nachlauf", () => {
    // iOS scrollt nach dem Loslassen bis zu rund zwei Sekunden weiter, ohne
    // dass ein weiteres Geraete-Ereignis kommt. Verfiele die Bewaffnung sofort,
    // wuerde der Verlauf mitten im Nachlauf wieder ans Ende gerissen.
    vi.useFakeTimers();
    try {
      mitBeobachter((el, ausloesen, setzeHoehe) => {
        setzeHoehe(950);
        el.scrollTop = 350;
        fireEvent.scroll(el);

        fireEvent.touchMove(el); // Wisch nach oben
        setzeHoehe(6000);
        el.scrollTop = 300;
        fireEvent.scroll(el);

        vi.advanceTimersByTime(800);
        el.scrollTop = 250;
        fireEvent.scroll(el); // Nachlauf, dieselbe Geste

        ausloesen();
        expect(el.scrollTop).toBe(250);
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("eine Geste, auf die kein Scroll folgt, verfaellt", () => {
    // Das eigentliche Leck: eine Geste, die NICHTS scrollt — Wisch am unteren
    // Anschlag, Wisch auf einem nicht scrollenden Bereich. Sie bewaffnete das
    // Flag, und weil kein Scroll-Ereignis folgte, raeumte auch der
    // `atBottom`-Zweig es nie wieder ab. Das naechste Reflow, Minuten spaeter,
    // erbte die Bewaffnung und schaltete das Mitlaufen ab.
    vi.useFakeTimers();
    try {
      mitBeobachter((el, ausloesen, setzeHoehe) => {
        setzeHoehe(950);
        el.scrollTop = 350;
        fireEvent.scroll(el); // am Ende, Mitlaufen an

        fireEvent.wheel(el, { deltaY: -400 }); // Geste ohne Wirkung
        vi.advanceTimersByTime(60_000);

        setzeHoehe(6000); // spaetes Reflow
        fireEvent.scroll(el);

        ausloesen();
        expect(el.scrollTop).toBe(6000);
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("beendet das Mitlaufen, wenn der Nutzer wirklich hochscrollt", () => {
    mitBeobachter((el, ausloesen) => {
      fireEvent.wheel(el, { deltaY: -400 });
      el.scrollTop = 350;
      fireEvent.scroll(el);

      ausloesen();
      expect(el.scrollTop).toBe(350);
    });
  });

  it("nimmt das Mitlaufen wieder auf, wenn der Nutzer ans Ende zurueckkehrt", () => {
    mitBeobachter((el, ausloesen) => {
      fireEvent.wheel(el, { deltaY: -400 });
      el.scrollTop = 350;
      fireEvent.scroll(el);

      // Zurueck ans Ende gescrollt — Geste gilt damit als beendet.
      el.scrollTop = 6000 - 600;
      fireEvent.scroll(el);

      ausloesen();
      expect(el.scrollTop).toBe(6000);
    });
  });

  // Regression: the mobile stack keeps the off-screen pane mounted with
  // `display: none`, where the scroll container measures 0 and the
  // scroll-to-bottom effect is a silent no-op. Nothing re-triggers it when the
  // pane becomes visible, so the chat opened at the very top of a long
  // history. A ResizeObserver on the container catches the box appearing.
  it("scrolls to the bottom when the timeline box changes size (pane became visible)", () => {
    const original = window.ResizeObserver;
    const observed: Element[] = [];
    let fire: (() => void) | null = null;
    class CapturingResizeObserver {
      constructor(cb: ResizeObserverCallback) {
        fire = () => cb([], this as unknown as ResizeObserver);
      }
      observe(el: Element) { observed.push(el); }
      unobserve() {}
      disconnect() {}
    }
    window.ResizeObserver = CapturingResizeObserver as unknown as typeof ResizeObserver;

    try {
      mockUseChatStream.mockReturnValue(mkStream({ events: [MSG] }));
      renderChatView();

      expect(observed).toHaveLength(1);
      const el = observed[0] as HTMLElement;
      // jsdom reports 0 for every layout metric, so stand in for a long history.
      Object.defineProperty(el, "scrollHeight", { value: 5000, configurable: true });
      el.scrollTop = 0;

      fire!();
      expect(el.scrollTop).toBe(5000);
    } finally {
      window.ResizeObserver = original;
    }
  });

  // ── Mobile stack header ───────────────────────────────────────────────────

  it("shows no back chevron when the caller has no list to go back to (desktop)", () => {
    mockUseChatStream.mockReturnValue(mkStream());
    renderChatView();
    expect(screen.queryByRole("button", { name: "Back to sessions" })).not.toBeInTheDocument();
  });

  it("the back chevron reports the intent to return to the list", async () => {
    mockUseChatStream.mockReturnValue(mkStream());
    const onBack = vi.fn();
    const user = userEvent.setup();
    renderChatView({ onBack });

    await user.click(screen.getByRole("button", { name: "Back to sessions" }));
    expect(onBack).toHaveBeenCalled();
  });

  it("shows the context line under the agent name", () => {
    mockUseChatStream.mockReturnValue(mkStream());
    renderChatView({ contextLine: "Login reparieren" });
    expect(screen.getByText("Login reparieren")).toBeInTheDocument();
  });

  it("keeps the options sheet closed until the header button is used", async () => {
    mockUseChatStream.mockReturnValue(mkStream());
    const user = userEvent.setup();
    renderChatView();

    expect(screen.queryByTestId("chat-options-sheet")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Chat options" }));
    expect(screen.getByTestId("chat-options-sheet")).toBeInTheDocument();
  });

  it("passes the effective view to the sheet, so a forced-terminal agent can't pick Chat there", async () => {
    mockUseChatStream.mockReturnValue(mkStream());
    const user = userEvent.setup();
    renderChatView({ hasTranscript: false, centerView: "chat" });

    await user.click(screen.getByRole("button", { name: "Chat options" }));
    expect(screen.getByRole("radio", { name: /Chat/ })).toBeDisabled();
    expect(screen.getByRole("radio", { name: /Terminal/ })).toHaveAttribute("aria-checked", "true");
  });

  // ── Optimistic echo ───────────────────────────────────────────────────────
  // The bubble must exist in the frame the send happens, not a tailer poll
  // later. ChatView's job here is narrow: echo BEFORE the request, drop the
  // echo if the request fails, render pending echoes last.

  it("echoes the message before the request is even dispatched", async () => {
    const echoSent = vi.fn();
    let sendResolved = false;
    vi.mocked(api.chat.sendText).mockImplementation(() => {
      // Asserted inside the request: the echo must already have happened.
      expect(echoSent).toHaveBeenCalledWith("los gehts");
      sendResolved = true;
      return Promise.resolve(undefined);
    });
    mockUseChatStream.mockReturnValue(mkStream({ echoSent }));
    const user = userEvent.setup();
    renderChatView();

    await user.type(screen.getByPlaceholderText("Message the agent…"), "los gehts");
    await user.click(screen.getByRole("button", { name: "Send" }));

    expect(sendResolved).toBe(true);
  });

  it("withdraws the echo when the send fails", async () => {
    const echoFailed = vi.fn();
    vi.mocked(api.chat.sendText).mockRejectedValue(new Error("API 500"));
    mockUseChatStream.mockReturnValue(mkStream({ echoFailed }));
    const user = userEvent.setup();
    renderChatView();

    await user.type(screen.getByPlaceholderText("Message the agent…"), "geht nicht");
    await user.click(screen.getByRole("button", { name: "Send" }));

    // A bubble that outlived a failed send would claim a delivery that never
    // happened — worse than the delay it was meant to hide.
    await waitFor(() => expect(echoFailed).toHaveBeenCalledWith("geht nicht"));
  });

  it("renders a pending echo as a dimmed bubble after the real timeline", () => {
    mockUseChatStream.mockReturnValue(
      mkStream({
        events: [MSG],
        pendingEchoes: [{ id: "echo-1", text: "gerade abgeschickt", sentAt: Date.now(), status: "pending" }],
      })
    );
    renderChatView();

    const bubble = screen.getByTestId("echo-bubble");
    expect(bubble).toHaveAttribute("data-echo-status", "pending");
    expect(bubble).toHaveTextContent("gerade abgeschickt");
    // After the confirmed content, because it is by definition the newest thing.
    expect(screen.getByText("Hallo!").compareDocumentPosition(bubble)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING
    );
  });

  it("says so when an echo stays unconfirmed instead of looking delivered", () => {
    mockUseChatStream.mockReturnValue(
      mkStream({
        pendingEchoes: [{ id: "echo-1", text: "keine Antwort", sentAt: Date.now() - 20_000, status: "unconfirmed" }],
      })
    );
    renderChatView();

    expect(screen.getByTestId("echo-bubble")).toHaveAttribute("data-echo-status", "unconfirmed");
    expect(screen.getByText("Nicht bestätigt — Terminal prüfen")).toBeInTheDocument();
  });

  it("does not show the empty state while an echo is on screen", () => {
    mockUseChatStream.mockReturnValue(
      mkStream({
        events: [],
        pendingEchoes: [{ id: "echo-1", text: "erste Nachricht", sentAt: Date.now(), status: "pending" }],
      })
    );
    renderChatView();

    expect(screen.queryByText("No messages yet")).not.toBeInTheDocument();
    expect(screen.getByTestId("echo-bubble")).toBeInTheDocument();
  });

  it('shows "Gesendet…" until the transcript shows a sign of the turn', () => {
    mockUseChatStream.mockReturnValue(
      mkStream({ awaitingResponse: true, state: { kind: "state", status: "idle", prompt: null } })
    );
    renderChatView();

    // Outranks the pane probe's stale "idle" — otherwise the line reads
    // "Bereit" one frame after the operator hit send.
    expect(screen.getByText("Gesendet…")).toBeInTheDocument();
    expect(screen.queryByText("Bereit")).not.toBeInTheDocument();
  });

  it("hands the server's capabilities to the composer", async () => {
    mockUseChatStream.mockReturnValue(
      mkStream({
        usage: {
          kind: "usage",
          uuid: "u9",
          ts: "2026-08-17T10:00:00Z",
          inputTokens: 10,
          outputTokens: 1,
          model: "claude-opus-5",
          effort: "high",
        },
        capabilities: { effortLevels: ["low", "high", "max"], canSwitchEffort: true },
      })
    );
    const user = userEvent.setup();
    renderChatView();

    await user.click(screen.getByTestId("effort-chip"));
    // Der Regler bekommt die Stufen des Servers 1:1 — Endpunkte und Spannweite
    // beweisen das, ohne die Bedienung nachzubauen (die testet Composer selbst).
    const slider = screen.getByTestId("effort-slider");
    expect(slider).toHaveAttribute("max", "2");
    expect(screen.getByTestId("effort-menu")).toHaveTextContent("low");
    expect(screen.getByTestId("effort-menu")).toHaveTextContent("max");
  });

  // ── Queued / starting echoes ──────────────────────────────────────────────

  it("shows a mid-turn send as queued, with no warning", () => {
    mockUseChatStream.mockReturnValue(
      mkStream({
        state: { kind: "state", status: "working", prompt: null },
        pendingEchoes: [
          { id: "echo-1", text: "mach danach noch X", sentAt: Date.now(), status: "queued" },
        ],
      })
    );
    renderChatView();

    expect(screen.getByTestId("echo-bubble")).toHaveAttribute("data-echo-status", "queued");
    expect(
      screen.getByText("Eingereiht — wird nach dem laufenden Zug gesendet")
    ).toBeInTheDocument();
    // The whole point: nothing here looks like a problem.
    expect(screen.queryByText("Nicht bestätigt — Terminal prüfen")).not.toBeInTheDocument();
  });

  it("shows a send waiting on a booting agent calmly", () => {
    mockUseChatStream.mockReturnValue(
      mkStream({
        pendingEchoes: [
          { id: "echo-1", text: "hallo", sentAt: Date.now(), status: "starting" },
        ],
      })
    );
    renderChatView();

    expect(screen.getByTestId("echo-bubble")).toHaveAttribute("data-echo-status", "starting");
    expect(screen.getByText("Agent startet — wird zugestellt…")).toBeInTheDocument();
    expect(screen.queryByText("Nicht bestätigt — Terminal prüfen")).not.toBeInTheDocument();
  });

  it("routes a 409 agent_starting to the calm state and a retry, not to a failure", async () => {
    const echoAgentStarting = vi.fn();
    const echoFailed = vi.fn();
    vi.mocked(api.chat.sendText).mockRejectedValue(
      new Error('API 409: {"reason":"agent_starting"}')
    );
    mockUseChatStream.mockReturnValue(mkStream({ echoAgentStarting, echoFailed }));
    const user = userEvent.setup();
    renderChatView();

    await user.type(screen.getByPlaceholderText("Message the agent…"), "start doch");
    await user.click(screen.getByTestId("send-button"));

    await waitFor(() => expect(echoAgentStarting).toHaveBeenCalled());
    expect(echoAgentStarting.mock.calls[0][0]).toBe("start doch");
    // Nothing withdrawn, nothing shouted about — the message is still coming.
    expect(echoFailed).not.toHaveBeenCalled();
  });

  it("retries the send through the very same path when asked to", async () => {
    const sendText = vi.mocked(api.chat.sendText);
    sendText.mockRejectedValue(new Error('API 409: {"reason":"agent_starting"}'));
    let retryFn: (() => void) | null = null;
    const echoAgentStarting = vi.fn((_text: string, retry: () => void) => {
      retryFn = retry;
    });
    mockUseChatStream.mockReturnValue(mkStream({ echoAgentStarting }));
    const user = userEvent.setup();
    renderChatView();

    await user.type(screen.getByPlaceholderText("Message the agent…"), "nochmal");
    await user.click(screen.getByTestId("send-button"));
    await waitFor(() => expect(retryFn).not.toBeNull());

    const callsBefore = sendText.mock.calls.length;
    sendText.mockResolvedValue(undefined);
    retryFn!();

    // Same api call, same arguments — the retry is not a second, subtly
    // different code path.
    await waitFor(() => expect(sendText.mock.calls.length).toBe(callsBefore + 1));
    expect(sendText.mock.calls.at(-1)).toEqual(["agent-1", "nochmal"]);
  });

  // ── Session badge semantics ───────────────────────────────────────────────

  it('shows the "live" badge for an active session', () => {
    mockUseChatStream.mockReturnValue(
      mkStream({ session: { sessionId: "s1", live: true, startedAt: null, aliveness: "active" } })
    );
    renderChatView();
    const badge = screen.getByTestId("session-badge");
    expect(badge).toHaveAttribute("data-aliveness", "active");
    expect(badge).toHaveTextContent("live");
  });

  it("shows no alarming word for an IDLE session — only a quiet dot", () => {
    mockUseChatStream.mockReturnValue(
      mkStream({ session: { sessionId: "s1", live: false, startedAt: null, aliveness: "idle" } })
    );
    renderChatView();
    const badge = screen.getByTestId("session-badge");
    expect(badge).toHaveAttribute("data-aliveness", "idle");
    expect(badge).toHaveTextContent("");
    expect(screen.queryByText("beendet")).not.toBeInTheDocument();
  });

  it('keeps "beendet" for a session the server actually calls ended', () => {
    mockUseChatStream.mockReturnValue(
      mkStream({ session: { sessionId: "s1", live: false, startedAt: null, aliveness: "ended" } })
    );
    renderChatView();
    expect(screen.getByTestId("session-badge")).toHaveTextContent("beendet");
    expect(
      screen.getByText("Session beendet — neue Nachricht startet die nächste Session")
    ).toBeInTheDocument();
  });

  it('does NOT say "beendet" on a stale mtime alone (old backend)', () => {
    // The operator's actual complaint: `live: false` with no server aliveness
    // used to render the finished-session treatment at a running CLI.
    mockUseChatStream.mockReturnValue(
      mkStream({ session: { sessionId: "s1", live: false, startedAt: null } })
    );
    renderChatView();
    expect(screen.getByTestId("session-badge")).toHaveAttribute("data-aliveness", "idle");
    expect(screen.queryByText("beendet")).not.toBeInTheDocument();
  });

  it("offers a Send (not a Stop) on an idle session — the morph follows the agent, not the session", () => {
    mockUseChatStream.mockReturnValue(
      mkStream({ session: { sessionId: "s1", live: false, startedAt: null, aliveness: "idle" } })
    );
    renderChatView();
    // Per the operator's single-button ruling: Stop only appears while the agent
    // is mid-turn. An idle session offers the (disabled) Send instead.
    expect(screen.getByTestId("send-button")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
  });

  it("removes both faces of the button once the session has genuinely ended", () => {
    mockUseChatStream.mockReturnValue(
      mkStream({ session: { sessionId: "s1", live: false, startedAt: null, aliveness: "ended" } })
    );
    renderChatView();
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("send-button")).not.toBeInTheDocument();
  });

  // ── Chunked first paint ───────────────────────────────────────────────────

  it("mounts the tail of a long transcript first, then the rest", async () => {
    // 120 alternating messages -> 120 items, well past the window.
    const many = Array.from({ length: 120 }, (_, i) =>
      mkMsg({ uuid: `m${i}`, text: `Nachricht ${i}`, role: i % 2 === 0 ? "assistant" : "user" })
    );
    mockUseChatStream.mockReturnValue(mkStream({ events: many }));
    renderChatView();

    // First commit: the end of the conversation is on screen, the beginning is
    // not — that is what makes the page answer immediately on a long history.
    expect(screen.getByText("Nachricht 119")).toBeInTheDocument();
    expect(screen.queryByText("Nachricht 0")).not.toBeInTheDocument();

    // One frame later the remainder joins, without the reader losing the end.
    await waitFor(() => expect(screen.getByText("Nachricht 0")).toBeInTheDocument());
    expect(screen.getByText("Nachricht 119")).toBeInTheDocument();
  });

  it("does not defer anything when the transcript is short", () => {
    mockUseChatStream.mockReturnValue(mkStream({ events: [MSG] }));
    renderChatView();
    // Below the window the slice is a no-op — no reason to make a short
    // conversation arrive in two steps.
    expect(screen.getByText("Hallo!")).toBeInTheDocument();
  });

  // ── Loading / empty states ────────────────────────────────────────────────

  it("shows a skeleton shaped like the timeline while history loads", () => {
    mockUseChatStream.mockReturnValue(mkStream({ loading: true }));
    renderChatView();
    expect(screen.getByTestId("timeline-skeleton")).toBeInTheDocument();
    expect(screen.getByText("Loading transcript…")).toBeInTheDocument();
  });

  it("names both ways forward in the empty state instead of just reporting emptiness", () => {
    mockUseChatStream.mockReturnValue(mkStream({ loading: false }));
    renderChatView();
    expect(screen.getByText("No messages yet")).toBeInTheDocument();
    expect(screen.getByText(/Write the first message to Cody/)).toBeInTheDocument();
    expect(screen.queryByTestId("timeline-skeleton")).not.toBeInTheDocument();
  });

  it("drops the skeleton as soon as there is anything to render", () => {
    mockUseChatStream.mockReturnValue(mkStream({ loading: true, events: [MSG] }));
    renderChatView();
    expect(screen.queryByTestId("timeline-skeleton")).not.toBeInTheDocument();
    expect(screen.getByText("Hallo!")).toBeInTheDocument();
  });

  it("shows a neutral placeholder when no agent is selected", () => {
    mockUseChatStream.mockReturnValue(mkStream());
    render(
      <ChatView
        agent={null}
        hasTranscript={false}
        detailLevel="normal"
        onDetailLevelChange={noop}
        centerView="chat"
        onCenterViewChange={noop}
      />
    );
    expect(screen.getByText("Pick a session in the sidebar.")).toBeInTheDocument();
  });

  // ══════════════════════════════════════════════════════════════════════════
  // Handy-Kopfzeile — mittiger Name, runde Knöpfe (19.08.2026)
  // ══════════════════════════════════════════════════════════════════════════


  describe("Desktop-Kopfzeile — Detailgrad eingeklappt", () => {
    beforeEach(() => {
      mockUseChatStream.mockReturnValue(mkStream());
    });

    it("zeigt statt drei Segmenten einen Knopf mit dem aktuellen Wert", () => {
      // Operator-Wunsch 19.08.2026: "diese viele buttons und switche rechts
      // oben irgendwie verpacken". Der Detailgrad wird einmal eingestellt und
      // selten angefasst — Chat/Terminal dagegen staendig, das bleibt offen.
      renderChatView({ detailLevel: "normal" });
      const trigger = screen.getByTestId("detail-level-trigger");
      expect(trigger.textContent).toContain("Normal");
      // Die anderen Stufen sind erst nach dem Klick da.
      expect(screen.queryByRole("option", { name: "Compact" })).toBeNull();
    });

    it("beschriftet Knopf und Liste aus dem Katalog, nicht deutsch fest verdrahtet", async () => {
      // Der Review-Befund zu PR #331: `aria-label="Detailgrad"` stand deutsch
      // in der englischen Standard-Oberflaeche. Vorleseprogramme lesen genau
      // dieses Label vor — es muss durch t() laufen wie jeder andere UI-Text
      // (docs/i18n.md). Der Test rendert gegen messages/en.json.
      const user = userEvent.setup();
      renderChatView({ detailLevel: "verbose" });

      expect(screen.getByTestId("detail-level-trigger")).toHaveAttribute(
        "aria-label",
        "Detail level: Verbose"
      );
      await user.click(screen.getByTestId("detail-level-trigger"));
      expect(screen.getByRole("listbox")).toHaveAttribute("aria-label", "Detail level");
    });

    it("oeffnet die Liste erst auf Klick und meldet die Wahl", async () => {
      const onDetailLevelChange = vi.fn();
      const user = userEvent.setup();
      renderChatView({ detailLevel: "normal", onDetailLevelChange });

      await user.click(screen.getByTestId("detail-level-trigger"));
      await user.click(screen.getByRole("option", { name: "Verbose" }));

      expect(onDetailLevelChange).toHaveBeenCalledWith("verbose");
    });

    it("markiert die aktive Stufe fuer Vorleseprogramme", async () => {
      const user = userEvent.setup();
      renderChatView({ detailLevel: "compact" });

      await user.click(screen.getByTestId("detail-level-trigger"));

      expect(screen.getByRole("option", { name: "Compact" })).toHaveAttribute("aria-selected", "true");
      expect(screen.getByRole("option", { name: "Normal" })).toHaveAttribute("aria-selected", "false");
    });

    it("gibt es im Terminal-Modus gar nicht", () => {
      // Der Detailgrad betrifft nur die geparste Chat-Ansicht.
      renderChatView({ centerView: "terminal" });
      expect(screen.queryByTestId("detail-level-trigger")).toBeNull();
    });

    it("laesst Chat/Terminal sichtbar", () => {
      renderChatView({});
      expect(screen.getByRole("button", { name: "Terminal" })).toBeTruthy();
      expect(screen.getByRole("button", { name: "Chat" })).toBeTruthy();
    });
  });

  // Reine Rechenfunktion — hier prueft der Test WIRKUNG, nicht Klassennamen.
  // jsdom rechnet kein Layout, aber diese Zahlen entscheiden ueber die
  // Ueberdeckung, und sie sind ohne Browser pruefbar.
  describe("Handy-Kopfzeile — Platz fuer den Titel", () => {
    const M = MOBILE_HEADER_METRICS;

    it("reserviert rechts so viel, wie die Knopfgruppe wirklich braucht", () => {
      // Vorher stand dort pauschal px-14 (56px). Im Browser gemessen
      // (390x844): die rechte Gruppe belegt mit dem Abzeichen "beendet"
      // 152px — die Aufgaben-Zeile lief 50px in sie hinein, und das Abzeichen
      // hat `relative z-10` samt deckendem Hintergrund, malte also ueber den
      // Namen.
      const r = headerSideReservation({ hasBack: true, badge: "ended" });
      expect(r.right).toBe(
        M.headerPaddingRight + M.control + M.gap + M.control + M.gap + M.badgeEnded
      );
      expect(r.right).toBe(152);
      expect(r.right).toBeGreaterThan(56);
    });

    it("gibt den Platz wieder frei, wenn das Abzeichen schrumpft", () => {
      const ended = headerSideReservation({ hasBack: true, badge: "ended" });
      const dot = headerSideReservation({ hasBack: true, badge: "dot" });
      const none = headerSideReservation({ hasBack: true, badge: "none" });
      expect(dot.right).toBeLessThan(ended.right);
      expect(none.right).toBeLessThan(dot.right);
      // Ohne Abzeichen bleiben genau Mikrofon + "…" plus Polster.
      expect(none.right).toBe(M.headerPaddingRight + M.control + M.gap + M.control);
    });

    it("reserviert links nur, was der Zurück-Pfeil braucht", () => {
      expect(headerSideReservation({ hasBack: true, badge: "none" }).left).toBe(
        M.headerPaddingLeft + M.control
      );
      expect(headerSideReservation({ hasBack: false, badge: "none" }).left).toBe(
        M.headerPaddingLeft
      );
    });

    it("lässt auf einem 390px-Telefon im schlimmsten Fall noch Text übrig", () => {
      // Der schlimmste Fall ist "beendet" + Zurück-Pfeil. Bliebe hier nichts
      // uebrig, waere die Reservierung zwar ueberdeckungsfrei, aber nutzlos —
      // der Name waere auf ein Ellipsen-Zeichen zusammengeschnitten.
      const r = headerSideReservation({ hasBack: true, badge: "ended" });
      expect(390 - r.left - r.right).toBeGreaterThanOrEqual(180);
    });
  });

  describe("Handy-Kopfzeile", () => {
    beforeEach(() => {
      mockUseChatStream.mockReturnValue(mkStream());
    });

    it("legt den Namen in einen eigenen Block, der aus dem Fluss genommen ist", () => {
      renderChatView({ onBack: vi.fn() });
      const title = screen.getByTestId("chat-header-title");
      // Absolut, damit die Knopfgruppen seine Position nicht verschieben; die
      // Zentrierung traegt `items-center` (siehe eigener Test).
      expect(title.className).toContain("absolute");
      expect(title.className).toContain("items-center");
      // `text-center` stand hier frueher — tote Klasse: die beiden Spannen
      // sind `whitespace`-frei umbruchlos und schrumpfen auf ihren Inhalt,
      // eine Textausrichtung hat daran nichts auszurichten (nachgemessen).
      expect(title.className).not.toContain("text-center");
    });

    it("hält oben den Notch frei, weil keine App-Leiste mehr darüber liegt", () => {
      renderChatView({ onBack: vi.fn() });
      const header = screen.getByTestId("chat-header");
      expect(header.className).toContain("pt-safe-top");
    });

    it("gibt Zurück und Optionen eine runde Form", () => {
      renderChatView({ onBack: vi.fn() });
      // Der Kreis ist das SICHTBARE Element im Knopf, nicht der Knopf selbst —
      // der ist die (groessere, unsichtbare) Trefferflaeche, siehe unten.
      expect(circleOf("Back to sessions").className).toContain("rounded-full");
      expect(circleOf("Chat options").className).toContain("rounded-full");
    });

    it("gibt beiden Knöpfen eine Trefferfläche von mindestens 44px", () => {
      // DESIGN.md („Mobile-Disziplin: Touch-Targets ≥44px", WCAG 2.5.5). Beim
      // Umbau auf runde Knoepfe waren beide auf 36px geschrumpft — das ist die
      // taegliche Bedienung des Betreibers auf dem Telefon.
      // Der sichtbare Kreis darf klein bleiben (36px), die Trefferflaeche des
      // <button> nicht: `min-w-touch`/`min-h-touch` sind die WCAG-Utilities
      // aus globals.css, der Kreis liegt als Kind mittig darin.
      renderChatView({ onBack: vi.fn() });
      for (const label of ["Back to sessions", "Chat options"]) {
        const btn = screen.getByLabelText(label);
        expect(btn.className).toContain("min-w-touch");
        expect(btn.className).toContain("min-h-touch");
      }
      // Zweite Haelfte der Zusicherung: die Utility muss auch wirklich 44px
      // sein. Ohne diese Zeile koennte jemand `.min-h-touch` auf 36px setzen
      // und alle Klassen-Pruefungen blieben gruen.
      expect(GLOBALS_CSS).toMatch(/\.min-h-touch\s*\{\s*min-height:\s*44px/);
      expect(GLOBALS_CSS).toMatch(/\.min-w-touch\s*\{\s*min-width:\s*44px/);
    });

    it("macht den Kopf durch die grössere Trefferfläche nicht höher", () => {
      // Die 44px ragen ueber `-m-1` (je 4px) in die Polsterung der Kopfzeile
      // hinein: Aussenmass bleibt 36px, der Kopf also
      // safe + 6px + 36px + 6px + 1px Linie = safe + 3.0625rem.
      // Genau diesen Wert fuehrt --mobile-chat-topbar-h, und daran haengen die
      // Handy-Blaetter (Optionen/Kontext) ihre Oberkante. Waere der Kopf
      // gewachsen, bliebe unter dem Blatt ein Streifen Gespraech stehen.
      renderChatView({ onBack: vi.fn() });
      for (const label of ["Back to sessions", "Chat options"]) {
        expect(screen.getByLabelText(label).className).toContain("-m-1");
      }
      expect(GLOBALS_CSS).toContain(
        "--mobile-chat-topbar-h: calc(env(safe-area-inset-top) + 3.0625rem)"
      );
    });

    it("zentriert den Namen NUR auf dem Handy, nicht auf dem Desktop", () => {
      // Operator-Befund 19.08.2026 (Screenshot): auf dem Desktop stand "Boss"
      // weiterhin mittig. Der Test prueft jetzt den MECHANISMUS, der die
      // Zentrierung wirklich traegt: auf dem Handy ist der Block eine Spalte
      // mit `items-center`, ab md eine Zeile mit `md:items-baseline`.
      //
      // Frueher stand hier `justify-center`. Das war eine tote Klasse — im
      // Browser nachgemessen (390x844): mit und ohne sie sitzt der Name bei
      // exakt x=120.4/w=149.2. Der Block ist `absolute` ohne `top`/`bottom`,
      // seine Hoehe ergibt sich aus dem Inhalt, es gibt nichts zu verteilen.
      // Der Test waere also rot geworden, sobald jemand richtig aufgeraeumt
      // haette.
      renderChatView({ onBack: vi.fn() });
      const title = screen.getByTestId("chat-header-title");
      expect(title.className).toContain("items-center");
      expect(title.className).toContain("md:items-baseline");
      expect(title.className).not.toContain("justify-center");
    });

    it("behält auf dem Handy einen Zugang zur Sprachbedienung", () => {
      // Der Chat-Schirm blendet die App-Leiste aus (AppShell `mobileChromeless`),
      // und deren Knopf war der EINZIGE Zugang zur Sprachbedienung auf dem
      // Handy — Sidebar ist `hidden md:flex`, ein Tastenkuerzel gibt es nicht.
      // Ohne Ersatz war die Sprachbedienung auf dem Telefon schlicht weg.
      renderChatView({ onBack: vi.fn() });
      const mic = screen.getByLabelText("Start voice assistant");
      expect(mic).toBeInTheDocument();
      // Nur auf dem Handy: auf dem Desktop steht der Knopf in der Sidebar,
      // zweimal derselbe Schalter waere ein Duplikat im Dokument.
      expect(mic.parentElement?.className).toContain("md:hidden");
    });

    it("zeigt die Aufgaben-Zeile mittig unter dem Namen", () => {
      renderChatView({ onBack: vi.fn(), contextLine: "Login reparieren" });
      const title = screen.getByTestId("chat-header-title");
      expect(title.textContent).toContain("Login reparieren");
    });
  });

});

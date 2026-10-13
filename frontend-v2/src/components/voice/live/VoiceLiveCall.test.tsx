/**
 * Voice PR 5 — the navigation mic calls the LEAD agent, and the call is
 * app-wide: it survives page changes, floats where no chat is on screen and
 * docks into the lead agent's chat. Feature off or no lead → the mic stays
 * Jarvis (no regression).
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import type { ProgressSnapshot } from "./progress";
import type { VoiceLiveEnv } from "./useVoiceLive";
import { resetVoiceLiveConfigCache } from "./useVoiceLiveConfig";
import { FloatingVoiceCall, VoiceLiveCallProvider, resetLeadTargetCache } from "./VoiceLiveCall";
import { NavVoiceButton } from "./NavVoiceButton";
import { VoiceLiveDock } from "./VoiceLive";

vi.mock("@/lib/api", () => ({
  api: { voiceLive: { config: vi.fn(), session: vi.fn(), defaultTarget: vi.fn() } },
}));
const router = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => router }));
const store = vi.hoisted(() => ({ activeBoardId: "board-1" as string | null }));
vi.mock("@/lib/store", () => ({ useAppStore: () => store }));
vi.mock("@/components/voice/VoiceWidget", () => ({
  VoiceButton: ({ label }: { label?: string }) => (
    <button type="button" data-testid="jarvis-button" aria-label={label ?? "Start voice assistant"}>
      jarvis
    </button>
  ),
}));
// The floating call listens along through the lead's own chat stream.
const listener = vi.hoisted(() => ({
  mounted: [] as string[],
  snap: { state: null, events: [], todos: null } as ProgressSnapshot,
}));
vi.mock("@/hooks/useChatStream", () => ({
  useChatStream: (agentId: string | null, enabled: boolean) => {
    if (agentId && enabled) listener.mounted.push(agentId);
    return { ...listener.snap, loading: false };
  },
}));

const config = vi.mocked(api.voiceLive.config);
const defaultTarget = vi.mocked(api.voiceLive.defaultTarget);

class FakeDC {
  readyState = "connecting";
  onopen: ((e: unknown) => void) | null = null;
  onmessage: ((e: unknown) => void) | null = null;
  sent: string[] = [];
  send = (s: string) => this.sent.push(s);
  close = vi.fn();
  open() {
    this.readyState = "open";
    this.onopen?.({});
  }
  emit(event: object) {
    this.onmessage?.({ data: JSON.stringify(event) });
  }
}

class FakePC {
  dc = new FakeDC();
  connectionState = "new";
  ontrack = null;
  onconnectionstatechange = null;
  remote: unknown = null;
  close = vi.fn();
  createDataChannel = () => this.dc as unknown as RTCDataChannel;
  addTrack = () => undefined;
  createOffer = async () => ({ type: "offer", sdp: "v=0 offer" });
  setLocalDescription = async () => undefined;
  setRemoteDescription = async (d: unknown) => {
    this.remote = d;
  };
}

function harness() {
  const pcs: FakePC[] = [];
  const mintSession = vi.fn(async (agentId: string) => ({
    client_secret: "ek_test",
    expires_at: 1,
    model: "gpt-realtime-2.1",
    voice: "marin",
    calls_url: "https://api.openai.com/v1/realtime/calls",
    agent_name: agentId,
    call_ref: `ref-${agentId}`,
  }));
  const runTool = vi.fn(async (_body: { agent_id: string; call_ref: string }) => ({
    output: { delivered: true },
    sent_text: "[per Stimme] tests",
    mid_turn: false,
  }));
  const progress = vi.fn(async (body: { kind: string }) => ({ text: `[MC] ${body.kind}`, agent_final: false }));
  const env: VoiceLiveEnv = {
    isSecureContext: () => true,
    getUserMedia: async () => ({ getTracks: () => [{ stop: vi.fn() }] }) as unknown as MediaStream,
    createPeerConnection: () => {
      const pc = new FakePC();
      pcs.push(pc);
      return pc as unknown as RTCPeerConnection;
    },
    createAudioElement: () => ({ srcObject: null, play: async () => undefined }) as unknown as HTMLAudioElement,
    mintSession,
    postOffer: vi.fn(async () => new Response("v=0 answer", { status: 201 })),
    runTool: runTool as unknown as VoiceLiveEnv["runTool"],
    progress: progress as unknown as VoiceLiveEnv["progress"],
    now: () => 1_000_000,
    setTimeout: () => 0 as unknown as ReturnType<typeof setTimeout>,
    clearTimeout: () => undefined,
  };
  return { env, pcs, mintSession, runTool, progress };
}

type Page = { kind: "tasks" } | { kind: "chat"; agentId: string; agentName: string };

/** A tiny AppShell: provider, the nav mic, one page, the floating slot. */
function Shell({
  env,
  page,
  onForwarded,
}: {
  env: VoiceLiveEnv;
  page: Page;
  onForwarded?: (text: string) => void;
}) {
  return (
    <VoiceLiveCallProvider env={env}>
      <nav>
        <NavVoiceButton jarvisLabel="Voice (Jarvis)" />
      </nav>
      {page.kind === "tasks" ? (
        <main data-testid="tasks-page">Tasks</main>
      ) : (
        <VoiceLiveDock
          key={page.agentId}
          agentId={page.agentId}
          agentName={page.agentName}
          onForwarded={onForwarded}
          env={env}
        >
          {(slot) => <div data-testid="composer">{slot}</div>}
        </VoiceLiveDock>
      )}
      <FloatingVoiceCall />
    </VoiceLiveCallProvider>
  );
}

const TASKS: Page = { kind: "tasks" };
const LEAD_CHAT: Page = { kind: "chat", agentId: "lead-1", agentName: "Chief" };
const OTHER_CHAT: Page = { kind: "chat", agentId: "dev-1", agentName: "Dev" };

async function startFromNav(h: ReturnType<typeof harness>) {
  const mic = await screen.findByTestId("nav-voice-button");
  await act(async () => {
    fireEvent.click(mic);
  });
  await waitFor(() => expect(h.pcs).toHaveLength(1));
  await waitFor(() => expect(h.pcs[0].remote).not.toBeNull());
  act(() => h.pcs[0].dc.open());
  return h.pcs[0];
}

beforeEach(() => {
  resetVoiceLiveConfigCache();
  resetLeadTargetCache();
  config.mockReset();
  defaultTarget.mockReset();
  router.push.mockReset();
  listener.mounted = [];
  listener.snap = { state: null, events: [], todos: null };
  store.activeBoardId = "board-1";
  config.mockResolvedValue({ enabled: true, model: "gpt-realtime-2.1", voice: "marin" });
  defaultTarget.mockResolvedValue({ agent_id: "lead-1", agent_name: "Chief" });
});

describe("the navigation mic talks to the lead agent", () => {
  it("is labelled with the lead's name from the data and calls the lead", async () => {
    const h = harness();
    render(<Shell env={h.env} page={TASKS} />);
    const mic = await screen.findByTestId("nav-voice-button");
    expect(mic).toHaveAccessibleName("Voice (Chief)");
    expect(screen.queryByTestId("jarvis-button")).toBeNull();
    expect(defaultTarget).toHaveBeenCalledWith("board-1");
    await startFromNav(h);
    expect(h.mintSession).toHaveBeenCalledTimes(1);
    expect(h.mintSession.mock.calls[0][0]).toBe("lead-1");
  });

  it("while a call runs, the mic opens that call's chat instead of a second call", async () => {
    const h = harness();
    render(<Shell env={h.env} page={TASKS} />);
    await startFromNav(h);
    const mic = screen.getByTestId("nav-voice-button");
    expect(mic).toHaveAttribute("data-active", "true");
    expect(mic).toHaveAccessibleName("Open call with Chief");
    fireEvent.click(mic);
    expect(router.push).toHaveBeenCalledWith("/sessions?agent=lead-1&enter=1");
    expect(h.mintSession).toHaveBeenCalledTimes(1);
  });
});

describe("fallback: the mic stays Jarvis", () => {
  it("when the live voice is off (404), with today's label", async () => {
    config.mockRejectedValue(Object.assign(new Error("404"), { status: 404 }));
    const h = harness();
    render(<Shell env={h.env} page={TASKS} />);
    await waitFor(() => expect(config).toHaveBeenCalled());
    await act(async () => undefined);
    expect(screen.getByTestId("jarvis-button")).toHaveAccessibleName("Voice (Jarvis)");
    expect(screen.queryByTestId("nav-voice-button")).toBeNull();
    expect(defaultTarget).not.toHaveBeenCalled();
  });

  it("when the fleet has no lead agent", async () => {
    defaultTarget.mockRejectedValue(Object.assign(new Error("no_lead"), { status: 404 }));
    const h = harness();
    render(<Shell env={h.env} page={TASKS} />);
    await waitFor(() => expect(defaultTarget).toHaveBeenCalled());
    await act(async () => undefined);
    expect(screen.getByTestId("jarvis-button")).toBeInTheDocument();
    expect(screen.queryByTestId("nav-voice-button")).toBeNull();
  });
});

describe("the call survives navigation", () => {
  it("floats on a page without a chat, docks into the lead's chat, floats again — one connection throughout", async () => {
    const h = harness();
    const forwarded = vi.fn();
    const r = render(<Shell env={h.env} page={TASKS} onForwarded={forwarded} />);
    const pc = await startFromNav(h);

    // Tasks page: compact floating bar, the voice listens along on its own.
    expect(screen.getByTestId("voice-call-floating")).toBeInTheDocument();
    expect(screen.getByTestId("voice-call-compact")).toHaveAttribute("data-agent", "lead-1");
    expect(screen.getByTestId("voice-compact-open")).toHaveAttribute("href", "/sessions?agent=lead-1&enter=1");
    expect(listener.mounted).toContain("lead-1");

    // Open the lead's chat: the call docks — full bar, no floating bar.
    r.rerender(<Shell env={h.env} page={LEAD_CHAT} onForwarded={forwarded} />);
    expect(screen.getByTestId("voice-call-bar")).toBeInTheDocument();
    expect(screen.queryByTestId("voice-call-floating")).toBeNull();
    expect(screen.queryByTestId("voice-mic-button")).toBeNull();

    // A forwarded message lands in the docked chat as usual.
    act(() => pc.dc.emit({ type: "input_audio_buffer.committed", item_id: "u1" }));
    act(() =>
      pc.dc.emit({
        type: "conversation.item.input_audio_transcription.completed",
        item_id: "u1",
        transcript: "lass die Tests laufen",
      }),
    );
    act(() =>
      pc.dc.emit({
        type: "response.output_item.done",
        item: { type: "function_call", call_id: "c1", name: "send_to_agent", arguments: '{"text":"Tests laufen lassen"}' },
      }),
    );
    await waitFor(() => expect(forwarded).toHaveBeenCalledWith("[per Stimme] tests", false));
    expect(h.runTool.mock.calls[0][0]).toMatchObject({ agent_id: "lead-1", call_ref: "ref-lead-1" });

    // Back to Tasks: still the same call.
    r.rerender(<Shell env={h.env} page={TASKS} onForwarded={forwarded} />);
    expect(screen.getByTestId("voice-call-floating")).toBeInTheDocument();
    expect(pc.close).not.toHaveBeenCalled();
    expect(h.pcs).toHaveLength(1);

    // Hang up from the floating bar ends it for real.
    fireEvent.click(screen.getByTestId("voice-compact-hangup"));
    expect(pc.close).toHaveBeenCalled();
    expect(screen.queryByTestId("voice-call-floating")).toBeNull();
  });

  it("another agent's chat shows the call as one compact row and offers no second mic", async () => {
    const h = harness();
    const r = render(<Shell env={h.env} page={TASKS} />);
    await startFromNav(h);
    r.rerender(<Shell env={h.env} page={OTHER_CHAT} />);
    expect(screen.getByTestId("voice-call-compact")).toHaveAttribute("data-agent", "lead-1");
    expect(screen.queryByTestId("voice-call-floating")).toBeNull();
    expect(screen.queryByTestId("voice-call-bar")).toBeNull();
    expect(screen.queryByTestId("voice-mic-button")).toBeNull();
    expect(screen.getByTestId("composer")).toBeInTheDocument();
  });

  it("the mic inside a chat still talks to THAT chat's agent", async () => {
    const h = harness();
    render(<Shell env={h.env} page={OTHER_CHAT} />);
    const mic = await screen.findByTestId("voice-mic-button");
    await act(async () => {
      fireEvent.click(mic);
    });
    await waitFor(() => expect(h.mintSession).toHaveBeenCalledTimes(1));
    expect(h.mintSession.mock.calls[0][0]).toBe("dev-1");
    expect(screen.getByTestId("voice-call-bar")).toBeInTheDocument();
  });
});

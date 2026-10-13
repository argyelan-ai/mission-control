/**
 * Live voice in the chat (voice PR 1 + 2) — the call driven end to end
 * through a fake browser (getUserMedia, RTCPeerConnection, fetch) plus the
 * UI states and the two tools relayed to MC.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { VoiceLiveDock } from "./VoiceLive";
import type { StateEvent, TimelineChatEvent } from "@/lib/chatTypes";
import { FINAL_SETTLE_MS, type ProgressSnapshot } from "./progress";
import { PROGRESS_TICK_MS, type VoiceLiveEnv } from "./useVoiceLive";
import { resetVoiceLiveConfigCache } from "./useVoiceLiveConfig";
import { DISCONNECT_GRACE_MS, MAX_CALL_MS } from "./voiceLiveCore";

vi.mock("@/lib/api", () => ({ api: { voiceLive: { config: vi.fn(), session: vi.fn() } } }));
const mockRequest = vi.mocked(api.voiceLive.config);

type Handler = ((e: unknown) => void) | null;

class FakeDC {
  readyState = "connecting";
  onopen: Handler = null;
  onmessage: Handler = null;
  sent: string[] = [];
  send = (s: string) => this.sent.push(s);
  close = vi.fn(() => {
    this.readyState = "closed";
  });
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
  ontrack: Handler = null;
  onconnectionstatechange: Handler = null;
  tracks: unknown[] = [];
  remote: unknown = null;
  close = vi.fn();
  createDataChannel = () => this.dc as unknown as RTCDataChannel;
  addTrack = (t: unknown) => void this.tracks.push(t);
  createOffer = async () => ({ type: "offer", sdp: "v=0 offer" });
  setLocalDescription = async () => undefined;
  setRemoteDescription = async (d: unknown) => {
    this.remote = d;
  };
  setState(s: string) {
    this.connectionState = s;
    this.onconnectionstatechange?.({});
  }
}

interface Harness {
  env: VoiceLiveEnv;
  pcs: FakePC[];
  trackStop: ReturnType<typeof vi.fn>;
  postOffer: ReturnType<typeof vi.fn>;
  mintSession: ReturnType<typeof vi.fn>;
  runTool: ReturnType<typeof vi.fn>;
  progress: ReturnType<typeof vi.fn>;
  clock: { t: number };
  timers: { fn: () => void; ms: number; cleared: boolean; ran?: boolean }[];
}

function harness(over: Partial<VoiceLiveEnv> = {}): Harness {
  const pcs: FakePC[] = [];
  const trackStop = vi.fn();
  const timers: Harness["timers"] = [];
  const postOffer = vi.fn(async () => new Response("v=0 answer", { status: 201 }));
  const mintSession = vi.fn(async () => ({
    client_secret: "ek_test",
    expires_at: 1,
    model: "gpt-realtime-2.1",
    voice: "marin",
    calls_url: "https://api.openai.com/v1/realtime/calls",
    agent_name: "Boss",
    call_ref: "ref-1",
  }));
  const runTool = vi.fn(async (body: { name: string }) =>
    body.name === "send_to_agent"
      ? { output: { delivered: true, when: "now", done: false }, sent_text: "[per Stimme] x", mid_turn: false }
      : { output: { state: "working", step: "2/3: Tests", last_tool: "Bash · npm test" }, sent_text: null, mid_turn: false },
  );
  const clock = { t: 1_000_000 };
  const progress = vi.fn(async (body: { kind: string }) => ({
    text: `[MC progress · agent_final=${body.kind === "final"}] ${body.kind}`,
    agent_final: body.kind === "final",
  }));
  const env: VoiceLiveEnv = {
    isSecureContext: () => true,
    getUserMedia: async () => ({ getTracks: () => [{ stop: trackStop }] }) as unknown as MediaStream,
    createPeerConnection: () => {
      const pc = new FakePC();
      pcs.push(pc);
      return pc as unknown as RTCPeerConnection;
    },
    createAudioElement: () => ({ srcObject: null, play: async () => undefined }) as unknown as HTMLAudioElement,
    mintSession,
    postOffer,
    runTool: runTool as unknown as VoiceLiveEnv["runTool"],
    progress: progress as unknown as VoiceLiveEnv["progress"],
    now: () => clock.t,
    setTimeout: (fn, ms) => {
      timers.push({ fn, ms, cleared: false });
      return (timers.length - 1) as unknown as ReturnType<typeof setTimeout>;
    },
    clearTimeout: (id) => {
      const t = timers[id as unknown as number];
      if (t) t.cleared = true;
    },
    ...over,
  };
  return { env, pcs, trackStop, postOffer, mintSession, runTool, progress, clock, timers };
}

function renderDock(env: VoiceLiveEnv, onForwarded?: (text: string, midTurn: boolean) => void) {
  return render(
    <VoiceLiveDock agentId="agent-1" agentName="Boss" env={env} onForwarded={onForwarded}>
      {(slot) => <div data-testid="composer">{slot}</div>}
    </VoiceLiveDock>,
  );
}

async function startCall(h: Harness, onForwarded?: (text: string, midTurn: boolean) => void) {
  renderDock(h.env, onForwarded);
  const mic = await screen.findByTestId("voice-mic-button");
  await act(async () => {
    fireEvent.click(mic);
  });
  await waitFor(() => expect(h.pcs).toHaveLength(1));
  await waitFor(() => expect(h.pcs[0].remote).not.toBeNull());
  return h.pcs[0];
}

beforeEach(() => {
  resetVoiceLiveConfigCache();
  mockRequest.mockReset();
  mockRequest.mockResolvedValue({ enabled: true, model: "gpt-realtime-2.1", voice: "marin" });
});

describe("feature switch", () => {
  it("no mic at all when the backend says the feature is off (404)", async () => {
    mockRequest.mockRejectedValue(new Error('API 404: {"detail":"voice_live_disabled"}'));
    renderDock(harness().env);
    await waitFor(() => expect(mockRequest).toHaveBeenCalled());
    expect(screen.getByTestId("composer")).toBeEmptyDOMElement();
    expect(screen.queryByTestId("voice-mic-button")).toBeNull();
  });

  it("mic appears when on, labelled with the agent", async () => {
    renderDock(harness().env);
    const mic = await screen.findByTestId("voice-mic-button");
    expect(mic).toHaveAccessibleName("Talk to Boss");
    expect(mic).toHaveAttribute("data-secure", "true");
  });
});

describe("a call", () => {
  it("connecting → listening → speaking, with the secret only sent to OpenAI", async () => {
    const h = harness();
    const pc = await startCall(h);
    expect(screen.getByTestId("voice-state")).toHaveTextContent("Connecting…");
    expect(screen.queryByTestId("voice-mic-button")).toBeNull();
    expect(h.mintSession).toHaveBeenCalledWith("agent-1", "en");
    expect(h.postOffer).toHaveBeenCalledWith("https://api.openai.com/v1/realtime/calls", "ek_test", "v=0 offer");
    expect(pc.remote).toEqual({ type: "answer", sdp: "v=0 answer" });

    act(() => pc.dc.open());
    expect(screen.getByTestId("voice-state")).toHaveTextContent("Listening");
    expect(screen.getByText("Say what Boss should do. The voice passes it on in the chat.")).toBeInTheDocument();

    act(() => pc.dc.emit({ type: "output_audio_buffer.started" }));
    expect(screen.getByTestId("voice-state")).toHaveTextContent("Speaking");
    act(() => pc.dc.emit({ type: "output_audio_buffer.stopped" }));
    expect(screen.getByTestId("voice-state")).toHaveTextContent("Listening");
  });

  it("shows the operator's words as 'by voice' bubbles and the voice's reply, without PR 1's 'not passed on' notice", async () => {
    const h = harness();
    const pc = await startCall(h);
    act(() => pc.dc.open());
    act(() =>
      pc.dc.emit({ type: "conversation.item.input_audio_transcription.completed", item_id: "u1", transcript: "Hallo, hörst du mich?" }),
    );
    act(() => pc.dc.emit({ type: "response.output_audio_transcript.done", item_id: "v1", transcript: "Ja, ich höre dich." }));

    const you = screen.getByTestId("voice-line-you");
    expect(you).toHaveTextContent("by voice");
    expect(you).toHaveTextContent("Hallo, hörst du mich?");
    const voice = screen.getByTestId("voice-line-voice");
    expect(voice).toHaveTextContent("Boss's voice");
    expect(voice).toHaveTextContent("Ja, ich höre dich.");
    expect(screen.queryByTestId("voice-not-sent")).toBeNull();
    expect(you).toHaveAttribute("data-forwarded", "false");
  });

  it("hang-up closes mic, data channel and connection and brings the mic back", async () => {
    const h = harness();
    const pc = await startCall(h);
    act(() => pc.dc.open());
    fireEvent.click(screen.getByTestId("voice-hangup"));
    expect(h.trackStop).toHaveBeenCalled();
    expect(pc.dc.close).toHaveBeenCalled();
    expect(pc.close).toHaveBeenCalled();
    expect(pc.dc.sent.map((s) => JSON.parse(s).type)).toContain("session.close");
    expect(screen.queryByTestId("voice-call-bar")).toBeNull();
    expect(screen.getByTestId("voice-mic-button")).toBeInTheDocument();
  });

  it("a hang-up during the mic prompt wins over the late grant", async () => {
    let grant: (s: MediaStream) => void = () => undefined;
    const stop = vi.fn();
    const h = harness({
      getUserMedia: () => new Promise<MediaStream>((r) => (grant = r)),
    });
    renderDock(h.env);
    const micBtn = await screen.findByTestId("voice-mic-button");
    await act(async () => fireEvent.click(micBtn));
    fireEvent.click(screen.getByTestId("voice-hangup"));
    await act(async () => grant({ getTracks: () => [{ stop }] } as unknown as MediaStream));
    expect(stop).toHaveBeenCalled();
    expect(h.mintSession).not.toHaveBeenCalled();
    expect(h.pcs).toHaveLength(0);
  });

  it("ends itself after the maximum call length", async () => {
    const h = harness();
    const pc = await startCall(h);
    act(() => pc.dc.open());
    const cap = h.timers.find((t) => t.ms === MAX_CALL_MS)!;
    act(() => cap.fn());
    expect(screen.getByTestId("voice-error")).toHaveAttribute("data-error", "max_length");
    expect(screen.getByTestId("voice-error")).toHaveTextContent("Call ended after 20 minutes.");
    expect(pc.close).toHaveBeenCalled();
  });
});

describe("error states", () => {
  it("no HTTPS: mic greyed, tap explains, no microphone or backend call", async () => {
    const gum = vi.fn();
    const h = harness({ isSecureContext: () => false, getUserMedia: gum });
    renderDock(h.env);
    const mic = await screen.findByTestId("voice-mic-button");
    await waitFor(() => expect(mic).toHaveAttribute("data-secure", "false"));
    expect(mic).toHaveAccessibleName("Microphone only works over HTTPS");
    await act(async () => fireEvent.click(mic));
    expect(screen.getByTestId("voice-error")).toHaveAttribute("data-error", "insecure");
    expect(screen.getByTestId("voice-error")).toHaveTextContent("only works over HTTPS");
    expect(screen.queryByTestId("voice-retry")).toBeNull();
    expect(gum).not.toHaveBeenCalled();
    expect(h.mintSession).not.toHaveBeenCalled();
  });

  it("mic denied: settings hint, no session minted", async () => {
    const h = harness({
      getUserMedia: async () => {
        throw Object.assign(new Error("denied"), { name: "NotAllowedError" });
      },
    });
    renderDock(h.env);
    const micBtn = await screen.findByTestId("voice-mic-button");
    await act(async () => fireEvent.click(micBtn));
    expect(screen.getByTestId("voice-error")).toHaveAttribute("data-error", "mic_denied");
    expect(screen.getByTestId("voice-error")).toHaveTextContent("Microphone access was denied");
    expect(h.mintSession).not.toHaveBeenCalled();
  });

  it("no OpenAI credit at session start", async () => {
    const h = harness({
      mintSession: async () => {
        throw new Error('API 402: {"detail":{"reason":"no_credits"}}');
      },
    });
    renderDock(h.env);
    const micBtn = await screen.findByTestId("voice-mic-button");
    await act(async () => fireEvent.click(micBtn));
    expect(screen.getByTestId("voice-error")).toHaveTextContent("No OpenAI credit left for the voice.");
    expect(h.trackStop).toHaveBeenCalled();
    expect(screen.queryByTestId("voice-retry")).toBeNull();
  });

  it("quota running out mid-call closes the connection", async () => {
    const h = harness();
    const pc = await startCall(h);
    act(() => pc.dc.open());
    act(() => pc.dc.emit({ type: "error", error: { type: "insufficient_quota", code: "insufficient_quota" } }));
    expect(screen.getByTestId("voice-error")).toHaveAttribute("data-error", "no_credits");
    await waitFor(() => expect(pc.close).toHaveBeenCalled());
    expect(h.trackStop).toHaveBeenCalled();
  });

  it("network: a connection that stays 'disconnected' for the grace period ends with a retry", async () => {
    const h = harness();
    const pc = await startCall(h);
    act(() => pc.dc.open());
    act(() => pc.setState("disconnected"));
    expect(screen.getByTestId("voice-state")).toHaveTextContent("Listening");
    const grace = h.timers.find((t) => t.ms === DISCONNECT_GRACE_MS)!;
    act(() => grace.fn());
    expect(screen.getByTestId("voice-error")).toHaveTextContent("Connection lost. The agent keeps working.");
    expect(pc.close).toHaveBeenCalled();
    // One tap to call again.
    await act(async () => fireEvent.click(screen.getByTestId("voice-retry")));
    await waitFor(() => expect(h.pcs).toHaveLength(2));
  });

  it("network: a short 'disconnected' that heals keeps the call", async () => {
    const h = harness();
    const pc = await startCall(h);
    act(() => pc.dc.open());
    act(() => pc.setState("disconnected"));
    act(() => pc.setState("connected"));
    const grace = h.timers.find((t) => t.ms === DISCONNECT_GRACE_MS)!;
    expect(grace.cleared).toBe(true);
    expect(screen.getByTestId("voice-state")).toHaveTextContent("Listening");
  });

  it("network: 'failed' ends the call at once", async () => {
    const h = harness();
    const pc = await startCall(h);
    act(() => pc.setState("failed"));
    expect(screen.getByTestId("voice-error")).toHaveAttribute("data-error", "network");
  });

  it("OpenAI refusing the offer is an error, not a hanging 'connecting'", async () => {
    const h = harness({ postOffer: vi.fn(async () => new Response("nope", { status: 400 })) });
    renderDock(h.env);
    const micBtn = await screen.findByTestId("voice-mic-button");
    await act(async () => fireEvent.click(micBtn));
    await waitFor(() => expect(screen.getByTestId("voice-error")).toHaveAttribute("data-error", "unavailable"));
  });

  it("a browser without working WebRTC ends in an error and releases the mic, never a stuck 'connecting'", async () => {
    const h = harness({
      createPeerConnection: () => {
        throw new TypeError("RTCPeerConnection is not supported");
      },
    });
    renderDock(h.env);
    const micBtn = await screen.findByTestId("voice-mic-button");
    await act(async () => fireEvent.click(micBtn));
    await waitFor(() => expect(screen.getByTestId("voice-error")).toHaveAttribute("data-error", "unavailable"));
    expect(h.trackStop).toHaveBeenCalled();
  });

  it("close leaves the error and shows the mic again", async () => {
    const h = harness({ isSecureContext: () => false });
    renderDock(h.env);
    const micBtn = await screen.findByTestId("voice-mic-button");
    await act(async () => fireEvent.click(micBtn));
    fireEvent.click(screen.getByTestId("voice-dismiss"));
    expect(screen.queryByTestId("voice-call-bar")).toBeNull();
    expect(screen.getByTestId("voice-mic-button")).toBeInTheDocument();
  });
});

// ── tools (voice PR 2) ───────────────────────────────────────────────────

function sentTypes(pc: FakePC) {
  return pc.dc.sent.map((x) => JSON.parse(x));
}

function sayAndCall(pc: FakePC, words: string, args: object, callId = "call-1", name = "send_to_agent") {
  act(() => pc.dc.emit({ type: "input_audio_buffer.committed", item_id: "u1" }));
  act(() => pc.dc.emit({ type: "conversation.item.input_audio_transcription.completed", item_id: "u1", transcript: words }));
  act(() => pc.dc.emit({ type: "response.created" }));
  act(() =>
    pc.dc.emit({
      type: "response.output_item.done",
      item: { type: "function_call", call_id: callId, name, arguments: JSON.stringify(args) },
    }),
  );
  act(() => pc.dc.emit({ type: "response.done", response: { status: "completed" } }));
}

describe("tools", () => {
  it("send_to_agent: relays to MC with the operator's verbatim words, echoes, answers the model, asks it to speak", async () => {
    const h = harness();
    const onForwarded = vi.fn();
    const pc = await startCall(h, onForwarded);
    act(() => pc.dc.open());
    sayAndCall(pc, "und schreib noch Y in die Notiz", { text: "Schreib Y in die Notiz" });

    await waitFor(() => expect(h.runTool).toHaveBeenCalledTimes(1));
    expect(h.runTool).toHaveBeenCalledWith({
      call_ref: "ref-1",
      agent_id: "agent-1",
      name: "send_to_agent",
      arguments: { text: "Schreib Y in die Notiz" },
      transcript: "und schreib noch Y in die Notiz",
    });
    await waitFor(() => expect(onForwarded).toHaveBeenCalledWith("[per Stimme] x", false));
    await waitFor(() => expect(sentTypes(pc).map((e) => e.type)).toContain("response.create"));
    const out = sentTypes(pc).find((e) => e.type === "conversation.item.create");
    expect(out.item).toEqual({
      type: "function_call_output",
      call_id: "call-1",
      output: JSON.stringify({ delivered: true, when: "now", done: false }),
    });
    expect(screen.getByTestId("voice-line-you")).toHaveAttribute("data-forwarded", "true");
    expect(screen.getByTestId("voice-line-forwarded")).toHaveTextContent("to Boss");
    // The agent token never reaches the browser; the OpenAI key never either.
    expect(JSON.stringify(h.runTool.mock.calls)).not.toMatch(/token|sk-/i);
  });

  it("waits for a transcription that lands after the function call", async () => {
    const h = harness();
    const pc = await startCall(h);
    act(() => pc.dc.open());
    act(() => pc.dc.emit({ type: "input_audio_buffer.committed", item_id: "u1" }));
    act(() =>
      pc.dc.emit({
        type: "response.output_item.done",
        item: { type: "function_call", call_id: "c1", name: "send_to_agent", arguments: '{"text":"Tests laufen lassen"}' },
      }),
    );
    await act(async () => undefined);
    expect(h.runTool).not.toHaveBeenCalled();
    act(() =>
      pc.dc.emit({ type: "conversation.item.input_audio_transcription.completed", item_id: "u1", transcript: "lass mal die Tests laufen" }),
    );
    await waitFor(() => expect(h.runTool).toHaveBeenCalledTimes(1));
    expect(h.runTool.mock.calls[0][0].transcript).toBe("lass mal die Tests laufen");
  });

  it("the same call announced twice (both OpenAI events) runs once", async () => {
    const h = harness();
    const pc = await startCall(h);
    act(() => pc.dc.open());
    sayAndCall(pc, "mach Y", { text: "Mach Y" });
    act(() =>
      pc.dc.emit({ type: "response.function_call_arguments.done", call_id: "call-1", name: "send_to_agent", arguments: '{"text":"Mach Y"}' }),
    );
    await waitFor(() => expect(h.runTool).toHaveBeenCalledTimes(1));
    await act(async () => undefined);
    expect(h.runTool).toHaveBeenCalledTimes(1);
  });

  it("words already delivered are not attached again", async () => {
    const h = harness();
    const pc = await startCall(h);
    act(() => pc.dc.open());
    sayAndCall(pc, "erstens", { text: "Erstens" }, "c1");
    await waitFor(() => expect(h.runTool).toHaveBeenCalledTimes(1));
    act(() => pc.dc.emit({ type: "input_audio_buffer.committed", item_id: "u2" }));
    act(() => pc.dc.emit({ type: "conversation.item.input_audio_transcription.completed", item_id: "u2", transcript: "zweitens" }));
    act(() =>
      pc.dc.emit({ type: "response.output_item.done", item: { type: "function_call", call_id: "c2", name: "send_to_agent", arguments: '{"text":"Zweitens"}' } }),
    );
    await waitFor(() => expect(h.runTool).toHaveBeenCalledTimes(2));
    expect(h.runTool.mock.calls[1][0].transcript).toBe("zweitens");
  });

  it("not delivered (MC refuses / agent offline): no echo, the model hears NOT delivered", async () => {
    const h = harness({
      runTool: vi.fn(async () => {
        throw new Error('API 403: {"detail":{"reason":"call_unknown"}}');
      }),
    });
    const onForwarded = vi.fn();
    const pc = await startCall(h, onForwarded);
    act(() => pc.dc.open());
    sayAndCall(pc, "mach Y", { text: "Mach Y" });
    await waitFor(() => expect(sentTypes(pc).some((e) => e.type === "conversation.item.create")).toBe(true));
    const out = JSON.parse(sentTypes(pc).find((e) => e.type === "conversation.item.create").item.output);
    expect(out.delivered).toBe(false);
    expect(out.reason).toBe("call_unknown");
    expect(out.say).toMatch(/^NOT delivered/);
    expect(onForwarded).not.toHaveBeenCalled();
    expect(screen.getByTestId("voice-line-you")).toHaveAttribute("data-forwarded", "false");
  });

  it("get_agent_status: no transcript wait, no echo, answer handed to the model", async () => {
    const h = harness();
    const onForwarded = vi.fn();
    const pc = await startCall(h, onForwarded);
    act(() => pc.dc.open());
    act(() =>
      pc.dc.emit({ type: "response.output_item.done", item: { type: "function_call", call_id: "s1", name: "get_agent_status", arguments: "{}" } }),
    );
    await waitFor(() => expect(h.runTool).toHaveBeenCalledTimes(1));
    expect(h.runTool.mock.calls[0][0]).toMatchObject({ name: "get_agent_status", transcript: "" });
    await waitFor(() => expect(sentTypes(pc).map((e) => e.type)).toContain("response.create"));
    expect(onForwarded).not.toHaveBeenCalled();
  });

  it("response.create waits until the running response is done", async () => {
    let release: () => void = () => undefined;
    const h = harness({
      runTool: vi.fn(
        () =>
          new Promise((r) => {
            release = () => r({ output: { state: "idle" }, sent_text: null, mid_turn: false });
          }),
      ) as unknown as VoiceLiveEnv["runTool"],
    });
    const pc = await startCall(h);
    act(() => pc.dc.open());
    act(() => pc.dc.emit({ type: "response.created" }));
    act(() =>
      pc.dc.emit({ type: "response.output_item.done", item: { type: "function_call", call_id: "s1", name: "get_agent_status", arguments: "{}" } }),
    );
    await waitFor(() => expect(screen.getByTestId("voice-state")).toHaveTextContent("Passing on…"));
    await act(async () => release());
    await waitFor(() => expect(sentTypes(pc).some((e) => e.type === "conversation.item.create")).toBe(true));
    expect(sentTypes(pc).map((e) => e.type)).not.toContain("response.create");
    act(() => pc.dc.emit({ type: "response.done", response: { status: "completed" } }));
    expect(sentTypes(pc).map((e) => e.type)).toContain("response.create");
  });

  it("unknown tool names are ignored (the voice has no stop or keys tool)", async () => {
    const h = harness();
    const pc = await startCall(h);
    act(() => pc.dc.open());
    act(() =>
      pc.dc.emit({ type: "response.output_item.done", item: { type: "function_call", call_id: "x", name: "stop_agent", arguments: "{}" } }),
    );
    await act(async () => undefined);
    expect(h.runTool).not.toHaveBeenCalled();
  });
});

describe("transcript box", () => {
  it("fades the top only once older lines have scrolled out", async () => {
    const h = harness();
    const pc = await startCall(h);
    act(() => pc.dc.open());
    act(() => pc.dc.emit({ type: "conversation.item.input_audio_transcription.completed", item_id: "u1", transcript: "eins" }));
    const box = screen.getByTestId("voice-transcript");
    expect(box).toHaveAttribute("data-scrolled", "false");
    expect(box.className).not.toContain("mask-image");
    Object.defineProperty(box, "scrollTop", { configurable: true, get: () => 40, set: () => undefined });
    fireEvent.scroll(box);
    expect(box).toHaveAttribute("data-scrolled", "true");
    expect(box.className).toContain("mask-image");
  });
});

// ── listening along (voice PR 3) ─────────────────────────────────────────

function Dock({ env, progress }: { env: VoiceLiveEnv; progress: ProgressSnapshot }) {
  return (
    <VoiceLiveDock agentId="agent-1" agentName="Boss" env={env} progress={progress}>
      {(slot) => <div data-testid="composer">{slot}</div>}
    </VoiceLiveDock>
  );
}

const st = (status: StateEvent["status"], question?: string): StateEvent => ({
  kind: "state",
  status,
  prompt: question ? { question, options: [] } : null,
});
const snapOf = (status: StateEvent["status"], events: TimelineChatEvent[] = []): ProgressSnapshot => ({
  state: st(status),
  events,
  todos: null,
});
const reply = (text: string, uuid = "r1"): TimelineChatEvent => ({
  kind: "message",
  uuid,
  ts: "2026-10-13T10:00:00Z",
  role: "assistant",
  text,
  model: null,
  sidechain: false,
});

async function listen(h: Harness, initial: ProgressSnapshot) {
  const r = render(<Dock env={h.env} progress={initial} />);
  const mic = await screen.findByTestId("voice-mic-button");
  await act(async () => {
    fireEvent.click(mic);
  });
  await waitFor(() => expect(h.pcs[0]?.remote).toBeTruthy());
  const pc = h.pcs[0];
  act(() => pc.dc.open());
  return { pc, update: (p: ProgressSnapshot) => r.rerender(<Dock env={h.env} progress={p} />) };
}

/** Runs the newest pending progress-clock tick (it re-arms itself). */
function runTick(h: Harness) {
  const t = [...h.timers].reverse().find((x) => x.ms === PROGRESS_TICK_MS && !x.cleared && !x.ran);
  expect(t).toBeTruthy();
  t!.ran = true;
  act(() => t!.fn());
}

/** Lets the response the tool answer asked for finish, so the floor is free. */
function finishResponse(pc: FakePC) {
  act(() => pc.dc.emit({ type: "response.created" }));
  act(() => pc.dc.emit({ type: "response.done", response: { status: "completed" } }));
}

const contextItems = (pc: FakePC) =>
  sentTypes(pc).filter((e) => e.type === "conversation.item.create" && e.item?.role === "system");

async function forward(h: Harness, pc: FakePC) {
  sayAndCall(pc, "lass die Schleife laufen", { text: "Lass die Schleife laufen" });
  await waitFor(() => expect(sentTypes(pc).map((e) => e.type)).toContain("response.create"));
  finishResponse(pc);
  h.clock.t += 5_000;
  pc.dc.sent.length = 0;
}

describe("listening along (voice PR 3)", () => {
  it("the agent starts on the forwarded request: one redacted system item, then the voice speaks", async () => {
    const h = harness();
    const { pc, update } = await listen(h, snapOf("idle"));
    expect(h.progress).not.toHaveBeenCalled(); // baseline: history is never news
    await forward(h, pc);

    update(snapOf("working"));
    await waitFor(() => expect(h.progress).toHaveBeenCalledTimes(1));
    expect(h.progress.mock.calls[0][0]).toEqual({ call_ref: "ref-1", agent_id: "agent-1", kind: "started" });
    await waitFor(() => expect(sentTypes(pc).map((e) => e.type)).toContain("response.create"));
    expect(contextItems(pc)[0].item).toEqual({
      type: "message",
      role: "system",
      content: [{ type: "input_text", text: "[MC progress · agent_final=false] started" }],
    });
    // the context item goes in BEFORE the voice is asked to speak
    const types = sentTypes(pc).map((e) => e.type);
    expect(types.indexOf("conversation.item.create")).toBeLessThan(types.indexOf("response.create"));

    act(() => pc.dc.emit({ type: "response.created" }));
    act(() => pc.dc.emit({ type: "output_audio_buffer.started" }));
    expect(screen.getByTestId("voice-call-bar")).toHaveAttribute("data-narrating", "true");
    expect(screen.getByTestId("voice-state")).toHaveTextContent("Telling progress");
    act(() => pc.dc.emit({ type: "response.done", response: { status: "completed" } }));
    expect(screen.getByTestId("voice-call-bar")).toHaveAttribute("data-narrating", "false");
  });

  it("never talks over the operator: the item goes in, the voice waits until the floor is free", async () => {
    const h = harness();
    const { pc, update } = await listen(h, snapOf("working"));
    // A busy agent at call start: one silent snapshot item, no speech.
    await waitFor(() => expect(contextItems(pc)).toHaveLength(1));
    expect(h.progress.mock.calls[0][0].kind).toBe("snapshot");
    expect(sentTypes(pc).map((e) => e.type)).not.toContain("response.create");
    pc.dc.sent.length = 0;
    act(() => pc.dc.emit({ type: "input_audio_buffer.speech_started" }));
    update({ ...snapOf("working"), state: st("permission_prompt", "Allow deploy?") });
    await waitFor(() => expect(contextItems(pc)).toHaveLength(1));
    expect(h.progress.mock.calls.at(-1)?.[0]).toMatchObject({ kind: "approval", question: "Allow deploy?" });
    h.clock.t += 3_000;
    runTick(h);
    expect(sentTypes(pc).map((e) => e.type)).not.toContain("response.create");

    act(() => pc.dc.emit({ type: "input_audio_buffer.speech_stopped" }));
    // The model answers the operator by itself first …
    act(() => pc.dc.emit({ type: "response.created" }));
    h.clock.t += 2_000;
    runTick(h);
    expect(sentTypes(pc).map((e) => e.type)).not.toContain("response.create");
    // … and only after that answer the waiting update is spoken.
    act(() => pc.dc.emit({ type: "response.done", response: { status: "completed" } }));
    expect(sentTypes(pc).filter((e) => e.type === "response.create")).toHaveLength(1);
  });

  it("final comes from the agent's real reply, through the progress clock", async () => {
    const h = harness();
    const { pc, update } = await listen(h, snapOf("idle"));
    await forward(h, pc);
    update(snapOf("working"));
    await waitFor(() => expect(h.progress).toHaveBeenCalledTimes(1));
    update(snapOf("idle", [reply("Die Schleife ist durch, 30 von 30. Sonst nichts.")]));
    runTick(h);
    expect(h.progress).toHaveBeenCalledTimes(1); // not settled yet
    h.clock.t += FINAL_SETTLE_MS;
    runTick(h);
    await waitFor(() => expect(h.progress).toHaveBeenCalledTimes(2));
    expect(h.progress.mock.calls[1][0]).toMatchObject({
      kind: "final",
      text: "Die Schleife ist durch, 30 von 30. Sonst nichts.",
    });
  });

  it("no-success guard: the voice says done before the agent did → correction item, spoken", async () => {
    const h = harness();
    const { pc } = await listen(h, snapOf("idle"));
    await forward(h, pc);
    act(() => pc.dc.emit({ type: "response.output_audio_transcript.done", item_id: "v1", transcript: "Erledigt, er ist fertig." }));
    await waitFor(() => expect(h.progress).toHaveBeenCalledWith(expect.objectContaining({ kind: "premature_success" })));
    await waitFor(() => expect(sentTypes(pc).map((e) => e.type)).toContain("response.create"));
    // the same item again is not corrected twice; an honest line never
    act(() => pc.dc.emit({ type: "response.output_audio_transcript.done", item_id: "v1", transcript: "Erledigt." }));
    act(() =>
      pc.dc.emit({ type: "response.output_audio_transcript.done", item_id: "v2", transcript: "Noch nicht fertig, er arbeitet." }),
    );
    await act(async () => undefined);
    expect(h.progress.mock.calls.filter((c) => c[0].kind === "premature_success")).toHaveLength(1);
  });

  it("after the agent's final the guard is quiet", async () => {
    const h = harness();
    const { pc, update } = await listen(h, snapOf("idle"));
    await forward(h, pc);
    update(snapOf("working"));
    update(snapOf("idle", [reply("Fertig.")]));
    h.clock.t += FINAL_SETTLE_MS;
    runTick(h);
    await waitFor(() => expect(h.progress).toHaveBeenCalledWith(expect.objectContaining({ kind: "final" })));
    act(() => pc.dc.emit({ type: "response.output_audio_transcript.done", item_id: "v9", transcript: "Er ist fertig." }));
    await act(async () => undefined);
    expect(h.progress.mock.calls.some((c) => c[0].kind === "premature_success")).toBe(false);
  });

  it("a tool's output never leaves the browser — only its name and title", async () => {
    const h = harness();
    const { pc, update } = await listen(h, snapOf("working"));
    const running: TimelineChatEvent = {
      kind: "tool",
      uuid: "t1",
      ts: "2026-10-13T10:00:00Z",
      name: "Bash",
      title: "for i in $(seq 30); do sleep 1; done",
      detail: { command: "secret-detail" },
      toolUseId: "use-1",
      result: null,
      status: "done",
      stats: null,
      sidechain: false,
    };
    update(snapOf("working", [running]));
    h.clock.t += 5_000;
    update(snapOf("working", [{ ...running, result: "SECRET-OUTPUT line 1\nline 2" }]));
    await waitFor(() => expect(h.progress.mock.calls.map((c) => c[0].kind)).toContain("tool_finished"));
    const wire = JSON.stringify(h.progress.mock.calls) + JSON.stringify(pc.dc.sent);
    expect(wire).not.toContain("SECRET-OUTPUT");
    expect(wire).not.toContain("secret-detail");
    expect(h.progress.mock.calls.find((c) => c[0].kind === "tool_started")?.[0].tool).toEqual({
      name: "Bash",
      title: "for i in $(seq 30); do sleep 1; done",
    });
  });

  it("MC unreachable for a progress line: nothing is sent, the call goes on", async () => {
    const h = harness({
      progress: vi.fn(async () => {
        throw new Error("API 429: {}");
      }) as unknown as VoiceLiveEnv["progress"],
    });
    const { pc, update } = await listen(h, snapOf("idle"));
    pc.dc.sent.length = 0;
    update(snapOf("working"));
    await act(async () => undefined);
    expect(contextItems(pc)).toHaveLength(0);
    expect(screen.getByTestId("voice-call-bar")).toHaveAttribute("data-status", "listening");
  });

  it("hang-up stops the progress clock", async () => {
    const h = harness();
    const { pc } = await listen(h, snapOf("working"));
    act(() => pc.dc.emit({ type: "session.created" }));
    fireEvent.click(screen.getByTestId("voice-hangup"));
    expect(h.timers.filter((t) => t.ms === PROGRESS_TICK_MS && !t.cleared && !t.ran)).toHaveLength(0);
  });
});

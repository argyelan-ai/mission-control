/**
 * Live voice in the chat (voice PR 1) — the call driven end to end through a
 * fake browser (getUserMedia, RTCPeerConnection, fetch) plus the UI states.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { VoiceLiveDock } from "./VoiceLive";
import type { VoiceLiveEnv } from "./useVoiceLive";
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
  timers: { fn: () => void; ms: number; cleared: boolean }[];
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
  return { env, pcs, trackStop, postOffer, mintSession, timers };
}

function renderDock(env: VoiceLiveEnv) {
  return render(
    <VoiceLiveDock agentId="agent-1" agentName="Boss" env={env}>
      {(slot) => <div data-testid="composer">{slot}</div>}
    </VoiceLiveDock>,
  );
}

async function startCall(h: Harness) {
  renderDock(h.env);
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
    expect(screen.getByText("Say something. The voice answers; the work stays in the chat.")).toBeInTheDocument();

    act(() => pc.dc.emit({ type: "output_audio_buffer.started" }));
    expect(screen.getByTestId("voice-state")).toHaveTextContent("Speaking");
    act(() => pc.dc.emit({ type: "output_audio_buffer.stopped" }));
    expect(screen.getByTestId("voice-state")).toHaveTextContent("Listening");
  });

  it("shows the operator's words as 'by voice' bubbles and the voice's reply, and says nothing was passed on", async () => {
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
    expect(screen.getByTestId("voice-not-sent")).toHaveTextContent("Not passed on to Boss yet");
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

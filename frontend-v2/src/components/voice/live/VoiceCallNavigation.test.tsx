/**
 * The live voice call survives a client-side route change (live finding
 * 13.10.2026 on PR #808): every page renders its OWN <AppShell>, so a
 * navigation unmounts the old shell and mounts a new one. The call must live
 * above that — in the root `Providers` — or it hangs up on every tab tap.
 *
 * Rendered here: the real root `Providers` around the real `AppShell`
 * (neighbours stubbed), with the shell remounted by a new `key` per route,
 * exactly as Next.js does when a page with its own AppShell takes over.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const route = vi.hoisted(() => ({ pathname: "/tasks", push: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: route.push }),
  usePathname: () => route.pathname,
  useSearchParams: () => new URLSearchParams(),
}));

const token = vi.hoisted(() => ({ value: "t" }));
vi.mock("@/lib/api", () => ({
  getToken: () => token.value,
  getStoredUser: () => ({ id: "u", name: "Operator", email: "op@example.test", role: "admin" }),
  setStoredUser: vi.fn(),
  api: {
    auth: { me: vi.fn(async () => ({ id: "u", name: "Operator", email: "op@example.test", role: "admin" })) },
    voiceLive: {
      config: vi.fn(async () => ({ enabled: true, model: "gpt-realtime-2.1", voice: "marin" })),
      defaultTarget: vi.fn(async () => ({ agent_id: "lead-1", agent_name: "Chief" })),
    },
  },
}));

vi.mock("@/lib/store", () => ({
  useAppStore: () => ({ setCurrentUser: vi.fn(), activeBoardId: "board-1" }),
}));
vi.mock("@/lib/boards", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/boards")>()),
  useBoardsBootstrap: () => {},
}));

// Root-provider side effects that are not under test.
vi.mock("@/hooks/useKeyboardInset", () => ({ useKeyboardInset: () => {} }));
vi.mock("@/hooks/usePageBeacon", () => ({ usePageBeacon: () => {} }));
vi.mock("@/lib/theme", () => ({ startThemeSync: () => () => {} }));
vi.mock("@/hooks/useKeyboardShortcuts", () => ({ useKeyboardShortcuts: vi.fn() }));

// AppShell's heavy neighbours: placeholders. The nav mic stands in for the
// app bar (that is where the operator taps it).
vi.mock("@/components/layout/MobileNav", async () => {
  const { NavVoiceButton } = await import("./NavVoiceButton");
  return {
    __esModule: true,
    default: () => <NavVoiceButton />,
    MobileNavProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  };
});
vi.mock("@/components/layout/MobileTabBar", () => ({ MobileTabBar: () => <div data-testid="mobile-tabbar" /> }));
vi.mock("@/components/layout/QuickSheet", () => ({ QuickSheet: () => null }));
vi.mock("@/components/layout/Sidebar", () => ({ __esModule: true, default: () => null }));
vi.mock("@/components/layout/AmbientBackground", () => ({ AmbientBackground: () => null }));
vi.mock("@/components/shared/CommandPalette", () => ({ __esModule: true, default: () => null }));
vi.mock("@/components/shared/ToastRenderer", () => ({ __esModule: true, default: () => null }));
vi.mock("@/components/voice/VoiceWidget", () => ({
  VoiceProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  VoiceOverlay: () => null,
  VoiceButton: () => <button type="button" data-testid="jarvis-button" />,
}));

// The floating bar listens along through the lead's chat stream.
vi.mock("@/hooks/useChatStream", () => ({
  useChatStream: () => ({ state: null, events: [], todos: null, loading: false }),
}));

// WebRTC + mic: a fake browser, swapped in for the real one (no network, no
// Node-specific globals — CI runs Node 22).
const rtc = vi.hoisted(() => ({
  pcs: [] as { close: ReturnType<typeof import("vitest").vi.fn>; remote: unknown; dc: { onopen: ((e: unknown) => void) | null; readyState: string } }[],
  mints: 0,
}));
vi.mock("./useVoiceLive", async (importOriginal) => {
  const real = await importOriginal<typeof import("./useVoiceLive")>();
  const env: import("./useVoiceLive").VoiceLiveEnv = {
    isSecureContext: () => true,
    getUserMedia: async () => ({ getTracks: () => [{ stop: () => undefined }] }) as unknown as MediaStream,
    createPeerConnection: () => {
      const dc = { readyState: "connecting", onopen: null, onmessage: null, send: () => undefined, close: () => undefined };
      const pc = {
        dc,
        connectionState: "new",
        ontrack: null,
        onconnectionstatechange: null,
        remote: null as unknown,
        close: vi.fn(),
        createDataChannel: () => dc,
        addTrack: () => undefined,
        createOffer: async () => ({ type: "offer", sdp: "v=0" }),
        setLocalDescription: async () => undefined,
        setRemoteDescription: async (d: unknown) => {
          pc.remote = d;
        },
      };
      rtc.pcs.push(pc as never);
      return pc as unknown as RTCPeerConnection;
    },
    createAudioElement: () => ({ srcObject: null, play: async () => undefined }) as unknown as HTMLAudioElement,
    mintSession: async (agentId: string) => {
      rtc.mints += 1;
      return {
        client_secret: "ek_test",
        expires_at: 1,
        model: "gpt-realtime-2.1",
        voice: "marin",
        calls_url: "https://api.openai.com/v1/realtime/calls",
        agent_name: agentId,
        call_ref: `ref-${agentId}`,
      };
    },
    postOffer: async () => new Response("v=0 answer", { status: 201 }),
    runTool: async () => ({ output: {}, sent_text: null, mid_turn: false }),
    progress: async () => ({ text: "", agent_final: false }),
    now: () => 1_000_000,
    setTimeout: () => 0 as unknown as ReturnType<typeof setTimeout>,
    clearTimeout: () => undefined,
  };
  return { ...real, browserVoiceLiveEnv: env };
});

import { Providers } from "@/app/providers";
import AppShell from "@/components/layout/AppShell";
import { resetVoiceLiveConfigCache } from "./useVoiceLiveConfig";
import { resetLeadTargetCache } from "./VoiceLiveCall";

/** One page = its own AppShell, remounted per route like Next.js does. */
function App({ path }: { path: string }) {
  return (
    <Providers>
      <AppShell key={path}>
        <div data-testid={`page-${path}`}>{path}</div>
      </AppShell>
    </Providers>
  );
}

beforeEach(() => {
  resetVoiceLiveConfigCache();
  resetLeadTargetCache();
  rtc.pcs = [];
  rtc.mints = 0;
  route.pathname = "/tasks";
  token.value = "t";
});

describe("the live call survives navigation", () => {
  it("a client-side route change remounts the page shell — the call, its connection and its floating bar stay", async () => {
    const r = render(<App path="/tasks" />);
    const mic = await screen.findByTestId("nav-voice-button");
    await act(async () => {
      fireEvent.click(mic);
    });
    await waitFor(() => expect(rtc.pcs).toHaveLength(1));
    await waitFor(() => expect(rtc.pcs[0].remote).not.toBeNull());
    act(() => {
      rtc.pcs[0].dc.readyState = "open";
      rtc.pcs[0].dc.onopen?.({});
    });
    expect(screen.getByTestId("voice-compact-state")).toHaveTextContent("Listening");
    const firstShellPage = screen.getByTestId("page-/tasks");

    // Tab bar → /sessions: Next mounts the next page's own AppShell.
    route.pathname = "/sessions";
    r.rerender(<App path="/sessions" />);
    expect(screen.getByTestId("page-/sessions")).toBeInTheDocument();
    expect(firstShellPage.isConnected).toBe(false); // the shell really remounted

    expect(screen.getByTestId("voice-call-floating")).toBeInTheDocument();
    expect(screen.getByTestId("voice-compact-state")).toHaveTextContent("Listening");
    expect(rtc.pcs[0].close).not.toHaveBeenCalled();
    expect(rtc.pcs).toHaveLength(1);
    expect(rtc.mints).toBe(1);

    // And on to a third page: still the same call.
    route.pathname = "/inbox";
    r.rerender(<App path="/inbox" />);
    expect(screen.getByTestId("voice-call-floating")).toBeInTheDocument();
    expect(rtc.pcs[0].close).not.toHaveBeenCalled();
  });

  it("signed out (login page): no voice lookups, the mic stays Jarvis", async () => {
    token.value = "";
    const { api } = await import("@/lib/api");
    vi.mocked(api.voiceLive.config).mockClear();
    route.pathname = "/login";
    render(
      <Providers>
        <div data-testid="login">login</div>
      </Providers>,
    );
    await act(async () => undefined);
    expect(api.voiceLive.config).not.toHaveBeenCalled();
  });
});

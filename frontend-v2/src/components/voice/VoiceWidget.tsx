"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { motion, AnimatePresence } from "framer-motion";
import { Mic, MicOff, PhoneOff, X } from "lucide-react";
import {
  RoomContext,
  RoomAudioRenderer,
  StartAudio,
  useVoiceAssistant,
  BarVisualizer,
  useConnectionState,
  useLocalParticipant,
} from "@livekit/components-react";
import { ConnectionState, DisconnectReason, Room, RoomEvent } from "livekit-client";
import "@livekit/components-styles";
import { request } from "@/lib/api";
import { C } from "@/lib/colors";
import { useVoiceDisplay } from "./useVoiceDisplay";
import type { DisplayCard } from "./cards/types";
import { MemoryCard } from "./cards/MemoryCard";
import { UrlCard } from "./cards/UrlCard";
import { FileCard } from "./cards/FileCard";
import { TaskCard } from "./cards/TaskCard";
import { VoicePreviewSheet } from "./VoicePreviewSheet";

// ────────────────────────────────────────────────────────────────────────────
// VoiceContext + Provider
// Hosts the LiveKit room instance, connection state, and drawer toggle.
// VoiceButton + VoiceDrawer are pure consumers — can be rendered anywhere
// in the UI without floating-position hacks.
// ────────────────────────────────────────────────────────────────────────────

interface VoiceContextValue {
  active: boolean;
  connecting: boolean;
  drawerOpen: boolean;
  error: string | null;
  room: Room;
  // Trigger button ref — shared between VoiceButton + VoiceOverlay so
  // the drawer sits right next to the mic icon via getBoundingClientRect
  // (sidebar left on desktop, top bar right on mobile).
  anchorRef: RefObject<HTMLButtonElement | null>;
  // Display cards that Jarvis pushes onto the drawer stack via show_* tools.
  cards: DisplayCard[];
  dismissCard: (id: string) => void;
  // Preview sheet — lifted into the provider so the operator can end the call
  // ("hang up") without the open note getting cleared away with it.
  previewCard: DisplayCard | null;
  openPreview: (card: DisplayCard) => void;
  closePreview: () => void;
  toggleButton: () => void;
  endSession: () => void;
  closeDrawer: () => void;
  dismissError: () => void;
}

const VoiceContext = createContext<VoiceContextValue | null>(null);

export function useVoiceContext(): VoiceContextValue {
  const ctx = useContext(VoiceContext);
  if (!ctx) throw new Error("Voice* components must be used inside <VoiceProvider>");
  return ctx;
}

interface VoiceTokenResponse {
  token: string;
  url: string;
  room: string;
  identity: string;
}

export function VoiceProvider({ children }: { children: ReactNode }) {
  const [active, setActive] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const anchorRef = useRef<HTMLButtonElement | null>(null);

  // Display-cards WS — only active during a voice session.
  const { cards, clear: clearCards } = useVoiceDisplay(active);
  const [dismissedIds, setDismissedIds] = useState<Set<string>>(() => new Set());
  const visibleCards = useMemo(
    () => cards.filter((c) => !dismissedIds.has(c.id)),
    [cards, dismissedIds],
  );
  const dismissCard = useCallback((id: string) => {
    setDismissedIds((prev) => {
      const next = new Set(prev);
      next.add(id);
      return next;
    });
  }, []);

  // Preview sheet at the provider level: survives endSession() so the operator
  // can end the call ("save tokens") while the note stays
  // visible to keep reading. Only a manual close clears it away.
  const [previewCard, setPreviewCard] = useState<DisplayCard | null>(null);
  const openPreview = useCallback((c: DisplayCard) => setPreviewCard(c), []);
  const closePreview = useCallback(() => setPreviewCard(null), []);

  // One room instance per page lifetime (StrictMode-safe)
  const room = useMemo(
    () =>
      new Room({
        adaptiveStream: false,
        dynacast: true,
      }),
    [],
  );

  // State sync with room events
  useEffect(() => {
    const onDisconnected = (reason?: DisconnectReason) => {
      console.log("[voice] disconnected, reason=", reason);
      if (reason !== undefined && reason !== DisconnectReason.CLIENT_INITIATED) {
        setError(`Verbindung abgebrochen (${DisconnectReason[reason] ?? reason})`);
      }
      setActive(false);
      setDrawerOpen(false);
    };
    const onStateChange = (state: ConnectionState) => {
      console.log("[voice] connection-state=", state);
      if (state === ConnectionState.Disconnected) {
        setActive(false);
        setDrawerOpen(false);
      }
    };
    room.on(RoomEvent.Disconnected, onDisconnected);
    room.on(RoomEvent.ConnectionStateChanged, onStateChange);
    return () => {
      room.off(RoomEvent.Disconnected, onDisconnected);
      room.off(RoomEvent.ConnectionStateChanged, onStateChange);
    };
  }, [room]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      void room.disconnect();
    };
  }, [room]);

  const startSession = useCallback(async () => {
    setConnecting(true);
    setError(null);
    try {
      const data = await request<VoiceTokenResponse>("/api/v1/voice/token", { method: "POST" });
      console.log("[voice] token received, room=", data.room, "url=", data.url);
      await room.connect(data.url, data.token);
      await room.localParticipant.setMicrophoneEnabled(true);
      console.log("[voice] connected + mic enabled");
      setActive(true);
      // Pop the panel open right away: a mic tap starts AND shows the call.
      // The drawer only mounts once `active` is true (see VoiceOverlay),
      // so setting it ahead of time here is safe — no flash before connect.
      setDrawerOpen(true);
    } catch (e) {
      console.error("[voice] start-session failed:", e);
      const msg = e instanceof Error ? e.message : String(e);
      if (msg.includes("permission") || msg.includes("NotAllowed")) {
        setError("Mikrofon-Zugriff verweigert. iPhone: Safari → A → Website-Einstellungen → Mikrofon → Erlauben.");
      } else if (msg.includes("Network") || msg.includes("ECONN") || msg.includes("WebSocket")) {
        setError("LiveKit-Server nicht erreichbar (network).");
      } else {
        setError(`Verbindung fehlgeschlagen: ${msg}`);
      }
      void room.disconnect();
    } finally {
      setConnecting(false);
    }
  }, [room]);

  const endSession = useCallback(() => {
    void room.disconnect();
    setActive(false);
    setDrawerOpen(false);
    clearCards();
    setDismissedIds(new Set());
  }, [room, clearCards]);

  const toggleButton = useCallback(() => {
    // Stale state guard: UI 'active' but room disconnected → hard reset
    if (active && room.state !== ConnectionState.Connected) {
      console.warn("[voice] stale active state (room=%s) — hard reset", room.state);
      setActive(false);
      setDrawerOpen(false);
      void startSession();
      return;
    }
    if (active) {
      setDrawerOpen((o) => !o);
    } else {
      void startSession();
    }
  }, [active, room, startSession]);

  const closeDrawer = useCallback(() => setDrawerOpen(false), []);
  const dismissError = useCallback(() => setError(null), []);

  const value: VoiceContextValue = {
    active,
    connecting,
    drawerOpen,
    error,
    room,
    anchorRef,
    cards: visibleCards,
    dismissCard,
    previewCard,
    openPreview,
    closePreview,
    toggleButton,
    endSession,
    closeDrawer,
    dismissError,
  };

  return <VoiceContext.Provider value={value}>{children}</VoiceContext.Provider>;
}

// ────────────────────────────────────────────────────────────────────────────
// VoiceButton — compact, can be rendered in the MobileNav header or sidebar
// ────────────────────────────────────────────────────────────────────────────

interface VoiceButtonProps {
  size?: number;
  variant?: "header" | "sidebar";
}

export function VoiceButton({ size = 36, variant = "header" }: VoiceButtonProps) {
  const { active, connecting, anchorRef, toggleButton } = useVoiceContext();

  return (
    <button
      ref={anchorRef}
      type="button"
      onClick={toggleButton}
      disabled={connecting}
      className="flex items-center justify-center rounded-full transition-all disabled:opacity-50 cursor-pointer hover:scale-105 active:scale-95"
      style={{
        width: size,
        height: size,
        minWidth: 44,
        minHeight: 44,
        backgroundColor: active
          ? "var(--color-accent-subtle, rgba(15,163,163,0.12))"
          : variant === "sidebar"
            ? "rgba(255, 255, 255, 0.04)"
            : "transparent",
        border: active
          ? "1px solid var(--color-accent, #0FA3A3)"
          : variant === "sidebar"
            ? "1px solid var(--color-border-subtle, rgba(255,255,255,0.06))"
            : "1px solid transparent",
        color: active ? "var(--color-accent-light, #14C4C4)" : "var(--color-text-secondary)",
      }}
      aria-label={active ? "Manage voice session" : "Start voice assistant"}
      title={active ? "Voice active — click for options" : "Start voice"}
    >
      {connecting ? (
        <div
          className="border-2 border-current/30 border-t-current rounded-full animate-spin"
          style={{ width: size * 0.4, height: size * 0.4 }}
        />
      ) : active ? (
        <SoundWaveBars />
      ) : (
        <Mic size={Math.round(size * 0.5)} />
      )}
    </button>
  );
}

function SoundWaveBars() {
  return (
    <div className="flex items-center gap-[2px] h-4">
      <span className="w-[3px] bg-current rounded-full animate-voice-bar-1" />
      <span className="w-[3px] bg-current rounded-full animate-voice-bar-2" />
      <span className="w-[3px] bg-current rounded-full animate-voice-bar-3" />
      <style jsx>{`
        @keyframes voice-bar-1 {
          0%, 100% { height: 30%; }
          50% { height: 90%; }
        }
        @keyframes voice-bar-2 {
          0%, 100% { height: 60%; }
          50% { height: 100%; }
        }
        @keyframes voice-bar-3 {
          0%, 100% { height: 40%; }
          50% { height: 80%; }
        }
        .animate-voice-bar-1 { animation: voice-bar-1 0.9s ease-in-out infinite; }
        .animate-voice-bar-2 { animation: voice-bar-2 0.7s ease-in-out infinite 0.15s; }
        .animate-voice-bar-3 { animation: voice-bar-3 0.8s ease-in-out infinite 0.3s; }
      `}</style>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// VoiceOverlay — error toast + RoomContext + drawer. Rendered once by the
// AppShell, behavior state comes from the context.
// ────────────────────────────────────────────────────────────────────────────

export function VoiceOverlay() {
  const {
    active, drawerOpen, error, room, anchorRef, cards, dismissCard,
    previewCard, openPreview, closePreview,
    endSession, closeDrawer, dismissError,
  } = useVoiceContext();

  return (
    <>
      {error && (
        <div
          className="fixed left-1/2 -translate-x-1/2 z-[60] px-4 py-3 rounded-lg shadow-lg text-sm max-w-sm md:left-auto md:right-4 md:translate-x-0"
          style={{
            top: "calc(env(safe-area-inset-top) + 4rem)",
            backgroundColor: C.error,
            color: C.textPrimary,
          }}
        >
          <div className="font-medium mb-1">Voice error</div>
          <div className="opacity-90">{error}</div>
          <button onClick={dismissError} className="mt-2 underline text-xs cursor-pointer">
            Close
          </button>
        </div>
      )}

      {active && (
        <RoomContext.Provider value={room}>
          <VoiceDrawer
            open={drawerOpen}
            anchorRef={anchorRef}
            cards={cards}
            onDismissCard={dismissCard}
            onPreviewCard={openPreview}
            onClose={closeDrawer}
            onEnd={endSession}
          />
          <RoomAudioRenderer />
          <StartAudio label="Audio aktivieren" />
        </RoomContext.Provider>
      )}

      {/* Preview sheet lives OUTSIDE the active block: the operator can end the
          call ("hang up") and keep reading the note in peace. */}
      <VoicePreviewSheet card={previewCard} onClose={closePreview} />
    </>
  );
}

interface DrawerPosition {
  top: number;
  left: number;
  origin: "top left" | "top right";
}

function VoiceDrawer({
  open,
  anchorRef,
  cards,
  onDismissCard,
  onPreviewCard,
  onClose,
  onEnd,
}: {
  open: boolean;
  anchorRef: RefObject<HTMLButtonElement | null>;
  cards: DisplayCard[];
  onDismissCard: (id: string) => void;
  onPreviewCard: (card: DisplayCard) => void;
  onClose: () => void;
  onEnd: () => void;
}) {
  const { state, audioTrack } = useVoiceAssistant();
  const connectionState = useConnectionState();
  const { localParticipant } = useLocalParticipant();
  const [muted, setMuted] = useState(false);
  const [position, setPosition] = useState<DrawerPosition | null>(null);
  const [mobile, setMobile] = useState(false);

  // Recompute the position from the anchor button on open + scroll + resize.
  // If the button is outside the viewport (e.g. the sidebar was collapsed
  // at the time of the click), we fall back to a centered mobile layout.
  useLayoutEffect(() => {
    if (!open) return;
    const compute = () => {
      const isMobile = window.innerWidth < 768;
      setMobile(isMobile);
      if (isMobile || !anchorRef.current) {
        setPosition(null);
        return;
      }
      const r = anchorRef.current.getBoundingClientRect();
      const DRAWER_WIDTH = 340;
      const GAP = 12;
      // Sidebar button (variant=sidebar) sits on the left → expand the panel
      // to the right of it. MobileNav button (variant=header) isn't visible
      // on desktop, so it's irrelevant here.
      const spaceRight = window.innerWidth - r.right - GAP;
      const dropRight = spaceRight >= DRAWER_WIDTH;
      setPosition({
        top: Math.max(r.top, 16),
        left: dropRight ? r.right + GAP : Math.max(r.left - DRAWER_WIDTH - GAP, 16),
        origin: dropRight ? "top left" : "top right",
      });
    };
    compute();
    window.addEventListener("resize", compute);
    window.addEventListener("scroll", compute, true);
    return () => {
      window.removeEventListener("resize", compute);
      window.removeEventListener("scroll", compute, true);
    };
  }, [open, anchorRef]);

  const toggleMute = useCallback(async () => {
    const newMuted = !muted;
    setMuted(newMuted);
    await localParticipant.setMicrophoneEnabled(!newMuted);
  }, [muted, localParticipant]);

  const isConnected = connectionState === ConnectionState.Connected;
  const stateLabel: Record<string, string> = {
    listening: "Listening",
    thinking: "Thinking",
    speaking: "Speaking",
    initializing: "Initializing",
    idle: "Ready",
  };
  const label = isConnected ? (stateLabel[state] ?? state) : "Connecting …";
  const isSpeaking = state === "speaking";
  const isThinking = state === "thinking";

  // Style — mobile stays centered at the top, desktop uses the anchor position
  // Wichtig: kein transform-Centering — Framer Motion animiert transform
  // (scale/y) und überschreibt ein translateX(-50%), das Panel ragt dann
  // rechts aus dem Viewport (iPhone-Befund Operator). Stattdessen
  // left+right+margin auto.
  const panelStyle: React.CSSProperties = mobile || !position
    ? {
        top: "calc(env(safe-area-inset-top) + 4.5rem)",
        left: "0.75rem",
        right: "0.75rem",
        marginInline: "auto",
        width: "calc(100vw - 1.5rem)",
        maxWidth: 340,
        // Never beyond the bottom of the viewport (otherwise the controls
        // get cut off). dvh respects the iOS Safari toolbar; the card
        // stack scrolls internally, header + controls always stay visible.
        maxHeight:
          "calc(100dvh - env(safe-area-inset-top) - 4.5rem - env(safe-area-inset-bottom) - 1rem)",
      }
    : {
        top: position.top,
        left: position.left,
        width: 340,
        transformOrigin: position.origin,
        maxHeight: `calc(100vh - ${position.top}px - 1rem)`,
      };

  if (typeof document === "undefined") return null;

  return createPortal(
    <AnimatePresence>
      {open && (
        <>
          {/* Backdrop — outside-click + subtle dim */}
          <motion.div
            className="fixed inset-0 z-[55]"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            onClick={onClose}
            style={{ background: "rgba(0,0,0,0.25)", backdropFilter: "blur(2px)" }}
          />

          {/* Glass Panel */}
          <motion.div
            className="fixed z-[56] rounded-2xl overflow-hidden flex flex-col"
            initial={{ opacity: 0, scale: 0.94, y: -6 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.96, y: -4 }}
            transition={{ type: "spring", stiffness: 360, damping: 28 }}
            style={{
              ...panelStyle,
              background: "rgba(13, 13, 15, 0.92)",
              backdropFilter: "blur(20px) saturate(160%)",
              WebkitBackdropFilter: "blur(20px) saturate(160%)",
              border: "1px solid rgba(255,255,255,0.08)",
              boxShadow:
                "0 24px 60px -16px rgba(0,0,0,0.7), 0 0 0 1px rgba(15,163,163,0.10), inset 0 1px 0 0 rgba(255,255,255,0.06)",
            }}
          >
            {/* Edge highlight — subtler "rim" at the top edge */}
            <div
              className="absolute inset-x-0 top-0 h-px pointer-events-none"
              style={{
                background:
                  "linear-gradient(90deg, transparent 0%, rgba(255,255,255,0.16) 50%, transparent 100%)",
              }}
            />
            {/* Subtler radial glow — changes color with state */}
            <div
              className="absolute -top-16 left-1/2 -translate-x-1/2 w-64 h-32 pointer-events-none transition-opacity duration-500"
              style={{
                opacity: isSpeaking ? 0.7 : isThinking ? 0.5 : 0.25,
                background:
                  `radial-gradient(circle at 50% 50%, ${C.accentSubtle} 0%, transparent 60%)`,
                filter: "blur(16px)",
              }}
            />

            {/* Header */}
            <div className="relative shrink-0 flex items-center justify-between px-4 py-3 border-b border-white/[0.06]">
              <div className="flex items-center gap-2.5 min-w-0">
                <StatusPulse connected={isConnected} speaking={isSpeaking} />
                <div className="flex flex-col leading-none min-w-0">
                  <span
                    className="text-[11px] font-medium tracking-wide truncate"
                    style={{ color: "var(--color-text-primary)" }}
                  >
                    Jarvis
                  </span>
                  <span
                    className="text-[10px] mt-0.5 truncate"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    {label}
                  </span>
                </div>
              </div>
              <button
                type="button"
                onClick={onClose}
                className="p-1.5 rounded-md hover:bg-white/5 transition-colors cursor-pointer"
                aria-label="Close"
              >
                <X size={14} style={{ color: "var(--color-text-muted)" }} />
              </button>
            </div>

            {/* Live BarVisualizer — more compact when cards are present, so
                the panel doesn't grow too tall. */}
            <div
              className={`relative shrink-0 px-5 ${cards.length > 0 ? "pt-4 pb-3" : "pt-6 pb-5"}`}
            >
              <div className={`w-full ${cards.length > 0 ? "h-12" : "h-20"} transition-all`}>
                <BarVisualizer
                  state={state}
                  barCount={7}
                  trackRef={audioTrack}
                  options={{ minHeight: 6 }}
                  style={
                    {
                      "--lk-fg": isSpeaking ? C.accentHover : `${C.accent}99`,
                      "--lk-bg": "transparent",
                    } as React.CSSProperties
                  }
                />
              </div>
            </div>

            {/* Display-cards stack — pushed by Jarvis' show_* tools.
                Scrollable at >3 cards so the drawer doesn't overflow the
                viewport. Newest on top. */}
            {cards.length > 0 && (
              <div
                className="relative px-2.5 pb-2 flex flex-col gap-1.5 scrollbar-none flex-1 min-h-0"
                style={{ maxHeight: 280, overflowY: "auto" }}
              >
                <AnimatePresence initial={false}>
                  {cards.map((card) => (
                    <motion.div
                      key={card.id}
                      layout
                      initial={{ opacity: 0, y: -6, scale: 0.97 }}
                      animate={{ opacity: 1, y: 0, scale: 1 }}
                      exit={{ opacity: 0, x: -12, scale: 0.95 }}
                      transition={{ type: "spring", stiffness: 320, damping: 26 }}
                    >
                      {card.kind === "memory" && (
                        <MemoryCard
                          data={card.data}
                          title={card.title}
                          onClose={() => onDismissCard(card.id)}
                          onPreview={() => onPreviewCard(card)}
                        />
                      )}
                      {card.kind === "url" && (
                        <UrlCard
                          data={card.data}
                          title={card.title}
                          onClose={() => onDismissCard(card.id)}
                        />
                      )}
                      {card.kind === "file" && (
                        <FileCard
                          data={card.data}
                          title={card.title}
                          onClose={() => onDismissCard(card.id)}
                          onPreview={() => onPreviewCard(card)}
                        />
                      )}
                      {card.kind === "task" && (
                        <TaskCard
                          data={card.data}
                          title={card.title}
                          onClose={() => onDismissCard(card.id)}
                          onPreview={() => onPreviewCard(card)}
                        />
                      )}
                    </motion.div>
                  ))}
                </AnimatePresence>
              </div>
            )}

            {/* Controls */}
            <div className="relative shrink-0 flex items-center justify-center gap-2.5 px-4 pb-4 pt-2">
              <button
                type="button"
                onClick={toggleMute}
                className="flex items-center justify-center w-10 h-10 rounded-full transition-all cursor-pointer hover:scale-105 active:scale-95"
                style={{
                  background: muted ? `${C.error}1F` : "rgba(255,255,255,0.04)",
                  border: `1px solid ${muted ? `${C.error}4D` : "rgba(255,255,255,0.06)"}`,
                  color: muted ? C.error : "var(--color-text-primary)",
                }}
                aria-label={muted ? "Enable microphone" : "Mute"}
                title={muted ? "Microphone is muted" : "Mute"}
              >
                {muted ? <MicOff size={15} /> : <Mic size={15} />}
              </button>
              <button
                type="button"
                onClick={onEnd}
                className="flex items-center justify-center w-10 h-10 rounded-full transition-all cursor-pointer hover:scale-105 active:scale-95"
                style={{
                  background: C.error,
                  color: C.textPrimary,
                  boxShadow: "0 4px 14px rgba(0,0,0,0.4)",
                }}
                aria-label="End"
                title="End call"
              >
                <PhoneOff size={15} />
              </button>
            </div>
          </motion.div>
          {/* VoicePreviewSheet renders separately from VoiceOverlay — so it
              stays in place when the operator presses "End Call" here. */}
        </>
      )}
    </AnimatePresence>,
    document.body,
  );
}

function StatusPulse({ connected, speaking }: { connected: boolean; speaking: boolean }) {
  const color = connected ? (speaking ? C.accentHover : C.online) : C.warning;
  return (
    <div className="relative flex items-center justify-center w-2.5 h-2.5">
      <div
        className="absolute inset-0 rounded-full"
        style={{ background: color, boxShadow: `0 0 8px ${color}aa` }}
      />
      {speaking && (
        <motion.div
          className="absolute inset-0 rounded-full pointer-events-none"
          initial={{ scale: 1, opacity: 0.6 }}
          animate={{ scale: 2.6, opacity: 0 }}
          transition={{ duration: 1.2, repeat: Infinity, ease: "easeOut" }}
          style={{ background: color }}
        />
      )}
    </div>
  );
}

// Backwards-compat default export (old: floating widget — being replaced by
// the Provider + Button + Overlay pattern in AppShell).
export default function VoiceWidget() {
  return null;
}

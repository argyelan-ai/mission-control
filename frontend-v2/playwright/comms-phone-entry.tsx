/**
 * Mount fixture for `comms-phone-shots.mjs` (ADR-100 stage 2d): the REAL
 * inbox page and the REAL `ChatView`. Data comes over the network; the
 * driver answers the endpoints per scene. Synthetic only.
 */
import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { NextIntlClientProvider } from "next-intl";
import InboxPage from "@/app/inbox/page";
import { ChatView } from "@/components/chat/ChatView";
import { VoiceProvider } from "@/components/voice/VoiceWidget";
import type { AgentWithState } from "@/components/chat/TerminalPanel";
import de from "../messages/de.json";
import en from "../messages/en.json";

declare global {
  interface Window {
    __mountComms?: (view: "inbox" | "chat", locale: "de" | "en") => void;
  }
}

const agent = {
  id: "a1",
  name: "worker",
  status: "working",
  harness: "omp",
  headless_chat: true,
  agent_runtime: "cli-bridge",
  current_task_id: null,
  hasTranscript: true,
} as unknown as AgentWithState;

function Chat() {
  const [detailLevel, setDetailLevel] = useState<"compact" | "normal" | "verbose">("normal");
  const [centerView, setCenterView] = useState<"chat" | "terminal">("chat");
  return (
    <VoiceProvider>
      <div className="flex flex-col h-[100dvh] overflow-hidden">
        <div className="flex flex-1 min-w-0 min-h-0 overflow-hidden flex-col" style={{ background: "var(--color-bg-surface)" }}>
          <ChatView
            agent={agent}
            hasTranscript
            detailLevel={detailLevel}
            onDetailLevelChange={setDetailLevel}
            centerView={centerView}
            onCenterViewChange={setCenterView}
          />
        </div>
      </div>
    </VoiceProvider>
  );
}

function App({ view, locale }: { view: "inbox" | "chat"; locale: "de" | "en" }) {
  const [qc] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }));
  return (
    <NextIntlClientProvider locale={locale} messages={(locale === "de" ? de : en) as never} timeZone="UTC">
      <QueryClientProvider client={qc}>{view === "inbox" ? <InboxPage /> : <Chat />}</QueryClientProvider>
    </NextIntlClientProvider>
  );
}

export function mount() {
  const root = createRoot(document.getElementById("root")!);
  window.__mountComms = (view, locale) => root.render(<App key={`${view}-${locale}`} view={view} locale={locale} />);
}

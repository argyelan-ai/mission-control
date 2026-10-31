/**
 * Mount fixture for `chat-honest-shots.mjs` (31.10.2026 chat package):
 * the REAL `ChatView` (with its own head bar) of an agent whose tasks run in
 * their own session. Data comes over the
 * network; the driver answers the chat endpoints per scene. Synthetic only.
 */
import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { NextIntlClientProvider } from "next-intl";
import { ChatView } from "@/components/chat/ChatView";
import { VoiceProvider } from "@/components/voice/VoiceWidget";
import type { AgentWithState } from "@/components/chat/TerminalPanel";
import de from "../messages/de.json";
import en from "../messages/en.json";

declare global {
  interface Window {
    __mountHonest?: (locale: "de" | "en") => void;
  }
}

const agent = {
  id: "a1",
  name: "agent-a",
  status: "working",
  harness: "omp",
  headless_chat: true,
  tasks_in_own_session: true,
  agent_runtime: "cli-bridge",
  current_task_id: null,
  hasTranscript: true,
} as unknown as AgentWithState;

function App({ locale }: { locale: "de" | "en" }) {
  const [detailLevel, setDetailLevel] = useState<"compact" | "normal" | "verbose">("normal");
  const [centerView, setCenterView] = useState<"chat" | "terminal">("chat");
  const [qc] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }));
  return (
    <NextIntlClientProvider locale={locale} messages={(locale === "de" ? de : en) as never}>
      <QueryClientProvider client={qc}>
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
      </QueryClientProvider>
    </NextIntlClientProvider>
  );
}

export function mount() {
  const root = createRoot(document.getElementById("root")!);
  window.__mountHonest = (locale) => root.render(<App key={locale} locale={locale} />);
}

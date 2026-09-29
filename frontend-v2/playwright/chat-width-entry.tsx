/**
 * Mount fixture for `chat-transcript-width.mjs`.
 *
 * Mounts the REAL `ChatView` — real markdown pipeline, real Tailwind classes —
 * under the real page chain from `app/sessions/page.tsx`, and exposes
 * `__mount` / `__measure` on `window`. Not a test and not a mock: the point is
 * that a width contract can only be judged against real layout (jsdom computes
 * no layout at all, which is why this cannot live in the vitest suite).
 *
 * Data comes in over the network, exactly like production: the driver
 * intercepts the chat-history endpoint and feeds it a fixture.
 */
import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { NextIntlClientProvider } from "next-intl";
import { ChatView } from "@/components/chat/ChatView";
import { VoiceProvider } from "@/components/voice/VoiceWidget";
import { useKeyboardInset } from "@/hooks/useKeyboardInset";
import type { AgentWithState } from "@/components/chat/TerminalPanel";
import en from "../messages/en.json";

declare global {
  interface Window {
    __mount?: (shell: "sessions" | "app") => void;
    __measure?: () => unknown;
    __fakeKeyboard?: () => void;
  }
}

const agent = {
  id: "a1",
  name: "boss-host",
  status: "working",
  type: "claude",
  hasTranscript: true,
} as unknown as AgentWithState;

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

function App({ shell }: { shell: "sessions" | "app" }) {
  const [detailLevel, setDetailLevel] = useState<"compact" | "normal" | "verbose">("verbose");
  const [centerView, setCenterView] = useState<"chat" | "terminal">("chat");
  // The real hook: the app mounts it in providers.tsx, and it is the only
  // writer of `--keyboard-inset`. The "app" shell carries `.app-shell-height`,
  // the one rule that reads the variable and shrinks the shell on iOS.
  useKeyboardInset();
  const view = (
    <ChatView
      agent={agent}
      hasTranscript
      detailLevel={detailLevel}
      onDetailLevelChange={setDetailLevel}
      centerView={centerView}
      onCenterViewChange={setCenterView}
    />
  );
  return (
    <NextIntlClientProvider locale="en" messages={en as never}>
      <QueryClientProvider client={qc}>
        <VoiceProvider>
          {shell === "app" ? (
            // AppShell root → main → page container → split → islands.
            // `min-w-0` on every flex ancestor is what decides whether a wide
            // row can push the column open, so the chain has to be complete.
            <div
              className="flex overflow-hidden relative app-shell-height"
              style={{ backgroundColor: "var(--color-p2-bg)" }}
              data-testid="app-shell"
            >
              <div className="flex flex-col flex-1 min-w-0 overflow-hidden relative z-10">
                <main className="flex-1 overflow-hidden flex flex-col main-content-pb px-4 md:px-6 lg:px-8 md:pt-6">
                  <div className="mx-auto w-full max-w-[1600px] flex flex-col flex-1 min-h-0">
                    <div className="flex flex-col flex-1 overflow-hidden -mx-4 -mb-4 md:mx-0 md:mb-0 md:mt-0">
                      <div className="flex flex-col md:flex-row flex-1 min-h-0 overflow-hidden md:p-2 md:gap-2">
                        <div
                          className="flex flex-1 min-w-0 min-h-0 overflow-hidden flex-col md:flex-row md:rounded-xl md:border md:border-[var(--color-border)]"
                          data-testid="chat-island"
                        >
                          <div
                            className="flex flex-1 min-w-0 min-h-0 overflow-hidden flex-col"
                            style={{ background: "var(--color-bg-surface)" }}
                            data-testid="chat-column"
                          >
                            {view}
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                </main>
              </div>
            </div>
          ) : (
            <div className="flex flex-col flex-1 overflow-hidden h-[100dvh]">
              <div className="flex flex-col md:flex-row flex-1 min-h-0 overflow-hidden">
                <div className="flex flex-1 min-w-0 min-h-0 overflow-hidden flex-col" data-testid="chat-island">
                  <div
                    className="flex flex-1 min-w-0 min-h-0 overflow-hidden flex-col"
                    style={{ background: "var(--color-bg-surface)" }}
                    data-testid="chat-column"
                  >
                    {view}
                  </div>
                </div>
              </div>
            </div>
          )}
        </VoiceProvider>
      </QueryClientProvider>
    </NextIntlClientProvider>
  );
}

export function mount() {
  const root = createRoot(document.getElementById("root")!);
  window.__mount = (shell) => root.render(<App key={shell} shell={shell} />);

  window.__measure = () => {
    const timeline = document.querySelector<HTMLElement>('[data-testid="chat-timeline"]');
    const scroller = timeline?.parentElement as HTMLElement | null;
    const shellEl = document.querySelector<HTMLElement>('[data-testid="app-shell"]');

    // An element whose OWN overflow-x is not `visible` scrolls or clips its
    // content itself — a fenced code block, the wide-table wrapper, a
    // `truncate` label, the `sr-only` span. Those may legitimately be wider
    // than the transcript; anything else that is wider is a leak that widens
    // the scroller. The element ITSELF counts, not just its ancestors.
    const selfContained = (el: HTMLElement) => {
      for (let p: HTMLElement | null = el; p && p !== scroller; p = p.parentElement) {
        const own = getComputedStyle(p);
        if (own.overflowX !== "visible" || own.textOverflow === "ellipsis") return true;
      }
      return false;
    };

    const leaks = Array.from(document.querySelectorAll<HTMLElement>("body *"))
      .filter((el) => !selfContained(el))
      .map((el) => ({ el, over: el.scrollWidth - el.clientWidth }))
      .filter((x) => x.over > 0)
      .map(({ el, over }) => ({
        tag: el.tagName.toLowerCase(),
        testid: el.getAttribute("data-testid"),
        cls: (el.getAttribute("class") ?? "").slice(0, 80),
        overflowWrap: getComputedStyle(el).overflowWrap,
        over,
        text: (el.textContent ?? "").slice(0, 40),
      }));

    return {
      innerWidth: window.innerWidth,
      scroller: scroller
        ? {
            scrollWidth: scroller.scrollWidth,
            clientWidth: scroller.clientWidth,
            overflow: scroller.scrollWidth - scroller.clientWidth,
            canScrollX: scroller.scrollWidth > scroller.clientWidth,
            scrollLeft: scroller.scrollLeft,
            scrollTop: scroller.scrollTop,
            scrollHeight: scroller.scrollHeight,
            clientHeight: scroller.clientHeight,
          }
        : null,
      leaks,
      appShellHeight: shellEl ? Math.round(shellEl.getBoundingClientRect().height) : null,
      keyboardInset: getComputedStyle(document.documentElement).getPropertyValue("--keyboard-inset").trim(),
      visualViewportHeight: window.visualViewport?.height ?? null,
      innerHeight: window.innerHeight,
    };
  };
}

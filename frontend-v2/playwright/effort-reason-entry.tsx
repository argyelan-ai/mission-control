/**
 * Mount fixture for `effort-reason-shots.mjs`.
 *
 * Mounts the REAL `Composer` with a locked effort chip (a headless ACP agent
 * whose harness offers no thinking level) at the bottom of a phone-height
 * column, in the requested language. Theme is set by the driver on <html>.
 * Not a test: real layout only exists in a real browser, and the question here
 * is "does the reason popover fit on a 390 px phone and read well".
 */
import React from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { Composer } from "@/components/chat/Composer";
import en from "../messages/en.json";
import de from "../messages/de.json";

declare global {
  interface Window {
    __mountEffortReason?: (opts: { locale: "en" | "de"; reason: string; harness: string | null }) => void;
  }
}

function App({ locale, reason, harness }: { locale: "en" | "de"; reason: string; harness: string | null }) {
  const capabilities = {
    effortLevels: [],
    canSwitchEffort: false,
    effort: null,
    effortShared: false,
    effortReason: reason,
    effortModel: null,
    effortHarness: harness,
  };
  return (
    <NextIntlClientProvider locale={locale} messages={(locale === "de" ? de : en) as never}>
      <div
        className="flex flex-col h-[100dvh] overflow-hidden"
        style={{ background: "var(--color-bg-surface)" }}
        data-testid="chat-column"
      >
        <div className="flex-1" />
        <Composer
          agentId="a1"
          usage={null}
          capabilities={capabilities}
          state={null}
          onSend={() => {}}
          onStop={() => {}}
        />
      </div>
    </NextIntlClientProvider>
  );
}

export function mount() {
  const root = createRoot(document.getElementById("root")!);
  window.__mountEffortReason = (opts) => root.render(<App key={JSON.stringify(opts)} {...opts} />);
}

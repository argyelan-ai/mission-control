/**
 * Mount fixture for `browser-live-toolbar.mjs`.
 *
 * Mounts the REAL `BrowserLiveView` (real Tailwind, real i18n catalog) inside
 * the same full-width sheet the Sessions page uses on a phone, with a fake
 * WebSocket the driver feeds server messages through (`window.__blvPush`). REST
 * (`/browser-live/targets`, the stream ticket) is answered by the driver's
 * request interception, exactly like production traffic.
 */
import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { NextIntlClientProvider } from "next-intl";
import { BrowserLiveView } from "@/components/shared/BrowserLiveView";
import en from "../messages/en.json";

declare global {
  interface Window {
    __blvMount?: (opts: { agentName: string; showAllTabs?: boolean }) => void;
    __blvPush?: (msg: unknown) => void;
    __blvFrame?: (title: string, url: string) => string;
    __blvMeasure?: () => unknown;
  }
}

class FakeWebSocket {
  static OPEN = 1;
  static last: FakeWebSocket | null = null;
  readyState = 1;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  constructor(public url: string) {
    FakeWebSocket.last = this;
    setTimeout(() => this.onopen?.(new Event("open")), 0);
  }
  send() {}
  close() {}
}
// @ts-expect-error -- fixture stub, not a full WebSocket
window.WebSocket = FakeWebSocket;

window.__blvPush = (msg) => {
  FakeWebSocket.last?.onmessage?.(new MessageEvent("message", { data: JSON.stringify(msg) }));
};

/** A plain JPEG "page" so the viewport shows something page-like. */
window.__blvFrame = (title, url) => {
  const c = document.createElement("canvas");
  c.width = 1280;
  c.height = 800;
  const g = c.getContext("2d")!;
  g.fillStyle = "#f0f0f2";
  g.fillRect(0, 0, c.width, c.height);
  g.fillStyle = "#fdfdff";
  g.fillRect(340, 160, 600, 340);
  g.fillStyle = "#222";
  g.font = "bold 44px sans-serif";
  g.fillText(title, 380, 250);
  g.font = "26px sans-serif";
  g.fillText(url, 380, 320);
  return c.toDataURL("image/jpeg", 0.8).split(",")[1];
};

window.__blvMeasure = () => {
  const picker = document.querySelector("#browser-live-target") as HTMLElement | null;
  const full = document.querySelector('[aria-label="Fullscreen"]') as HTMLElement | null;
  const bar = picker?.parentElement as HTMLElement | null;
  const r = (el: HTMLElement | null) => (el ? el.getBoundingClientRect() : null);
  return {
    picker: r(picker),
    fullscreen: r(full),
    toolbar: r(bar),
    docOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  };
};

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

window.__blvMount = ({ agentName, showAllTabs }) => {
  try {
    if (showAllTabs) localStorage.setItem("mc.browserLive.showAllTabs.a1", "1");
    else localStorage.removeItem("mc.browserLive.showAllTabs.a1");
  } catch {
    // per-viewer convenience only
  }
  createRoot(document.getElementById("root")!).render(
    <QueryClientProvider client={qc}>
      <NextIntlClientProvider locale="en" messages={en}>
        {/* Same chain as the Sessions page's phone sheet: full width, a
            panel title row, then the panel body. */}
        <div className="fixed inset-x-0 bottom-0 top-0 flex flex-col overflow-hidden" style={{ background: "var(--color-bg-surface)" }}>
          <div className="flex items-center justify-between px-4 py-3 border-b shrink-0" style={{ borderColor: "var(--color-border)" }}>
            <span className="text-[14px] font-semibold">Browser</span>
          </div>
          <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
            <BrowserLiveView agentId="a1" agentName={agentName} />
          </div>
        </div>
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
};

export function mount() {
  // The driver calls window.__blvMount once it has set its routes up.
}

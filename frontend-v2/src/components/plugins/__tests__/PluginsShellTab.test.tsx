import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// Merely opening the Installer tab used to POST /plugins/shell — i.e. start
// a shell session — before anyone clicked "Start installer".

vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    cols = 80;
    rows = 24;
    loadAddon() {}
    open() {}
    write() {}
    writeln() {}
    clear() {}
    dispose() {}
    onData() {
      return { dispose() {} };
    }
  },
}));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: class { fit() {} } }));
vi.mock("@xterm/xterm/css/xterm.css", () => ({}));

const apiMock = vi.hoisted(() => ({
  plugins: {
    startShell: vi.fn(async () => ({ ok: true, session: "plugins-shell" })),
    stopShell: vi.fn(async () => ({ ok: true, session: "plugins-shell" })),
    shellWsUrl: () => "ws://localhost/api/v1/plugins/shell/ws?token=x",
  },
}));
vi.mock("@/lib/api", () => ({ api: apiMock }));

import { PluginsShellTab } from "../PluginsShellTab";

beforeEach(() => {
  apiMock.plugins.startShell.mockClear();
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  (globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
    static OPEN = 1;
    readyState = 0;
    binaryType = "";
    send() {}
    close() {}
  };
});

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <PluginsShellTab />
    </QueryClientProvider>
  );
}

describe("PluginsShellTab — shell starts only on request", () => {
  it("opening the tab does not start a shell", async () => {
    renderTab();
    await new Promise((r) => setTimeout(r, 50));
    expect(apiMock.plugins.startShell).not.toHaveBeenCalled();
  });

  it("'Start installer' starts it", async () => {
    renderTab();
    await userEvent.click(screen.getByRole("button", { name: /Start installer/i }));
    await waitFor(() => expect(apiMock.plugins.startShell).toHaveBeenCalledTimes(1));
  });
});

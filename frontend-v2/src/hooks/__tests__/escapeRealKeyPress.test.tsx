/**
 * A REAL Esc key press must close dialogs while the app's global shortcut hook
 * is mounted (as it is inside AppShell).
 *
 * Found by the open-everything UI probe: a real Esc did not close the
 * "New agent" wizard (/agents) nor the "Reference files" dialog (/tasks),
 * while a synthetic window keydown did. See browserKeydownCheckpoints.ts for
 * why a plain fireEvent/userEvent cannot show the difference on its own.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";
import { readFileSync } from "node:fs";
import path from "node:path";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useAppStore } from "@/lib/store";
import { useKeyboardShortcuts } from "@/hooks/useKeyboardShortcuts";
import { AgentWizard } from "@/app/agents/wizard/AgentWizard";
import { ProjectReferencesDialog } from "@/components/tasks/ProjectReferencesDialog";
import { emulateBrowserKeydownCheckpoints } from "./browserKeydownCheckpoints";

// The app store persists through localStorage; give it an in-memory one before
// the store module is created (Node 25 ships a broken global localStorage).
vi.hoisted(() => {
  const mem: Record<string, string> = {};
  Object.defineProperty(globalThis, "localStorage", {
    value: {
      getItem: (k: string) => mem[k] ?? null,
      setItem: (k: string, v: string) => { mem[k] = v; },
      removeItem: (k: string) => { delete mem[k]; },
      clear: () => undefined,
    },
    configurable: true,
    writable: true,
  });
});

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    agents: { list: vi.fn(async () => []) },
    boards: {},
    runtimes: {
      compatMatrix: vi.fn(async () => ({ harnesses: [], host_harnesses: [], runtimes: [] })),
      list: vi.fn(async () => ({ runtimes: [] })),
    },
    cliBridge: { health: vi.fn(async () => ({ reachable: true, bridge_url: "x" })) },
    agentTemplates: { list: vi.fn(async () => []) },
    plugins: { list: vi.fn(async () => ({ plugins: [], total: 0 })) },
    models: { list: vi.fn(async () => ({ models: [] })) },
    references: { list: vi.fn(async () => []), upload: vi.fn(), remove: vi.fn() },
  },
}));

/** Stand-in for AppShell: mounts the global shortcut hook like the app does. */
function Shell({ children }: { children: React.ReactNode }) {
  useKeyboardShortcuts();
  return <>{children}</>;
}

/** Page shape shared by /agents and /tasks: reads the app store and hands the
 *  dialog an inline (per-render) onClose. The dialog opens on a click AFTER the
 *  shell is mounted — like in the app, so the shell's keydown listener is
 *  registered before the dialog's. */
function WizardPage({ onClosed }: { onClosed: () => void }) {
  const { activeBoardId } = useAppStore();
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>Open</button>
      {open && (
        <AgentWizard
          boards={[]}
          defaultBoardId={activeBoardId}
          onClose={() => { onClosed(); setOpen(false); }}
          onCreated={() => setOpen(false)}
        />
      )}
    </>
  );
}

function ReferencesPage({ onClosed }: { onClosed: () => void }) {
  const { activeBoardId } = useAppStore();
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>Open</button>
      <ProjectReferencesDialog
        open={open}
        onClose={() => { onClosed(); setOpen(false); }}
        projectId={activeBoardId ?? "p1"}
        projectName="Demo project"
      />
    </>
  );
}

function mount(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <Shell>{ui}</Shell>
    </QueryClientProvider>,
  );
}

let browser: ReturnType<typeof emulateBrowserKeydownCheckpoints>;

beforeEach(() => {
  browser = emulateBrowserKeydownCheckpoints();
  useAppStore.setState({ commandPaletteOpen: false });
});

afterEach(() => {
  browser.restore();
});

describe("a real Esc key press closes dialogs under the global shortcut hook", () => {
  it("closes the New agent wizard", async () => {
    const onClosed = vi.fn();
    mount(<WizardPage onClosed={onClosed} />);
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(screen.getByRole("dialog", { name: "New agent" })).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");
    await browser.settled();

    expect(onClosed).toHaveBeenCalledTimes(1);
  });

  it("closes the Reference files dialog", async () => {
    const onClosed = vi.fn();
    mount(<ReferencesPage onClosed={onClosed} />);
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(screen.getByRole("dialog", { name: /References for Demo project/ })).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");
    await browser.settled();

    expect(onClosed).toHaveBeenCalledTimes(1);
  });

  it("still closes the command palette on Esc", async () => {
    useAppStore.setState({ commandPaletteOpen: true });
    mount(<div />);

    await userEvent.keyboard("{Escape}");
    await browser.settled();

    expect(useAppStore.getState().commandPaletteOpen).toBe(false);
  });

  it("keeps a dialog's Esc even when another keydown listener re-renders the page mid-press", async () => {
    // Any other window keydown listener that updates state (not just the
    // palette shortcut) must not be able to swallow a dialog's Esc.
    const rerenderOnKey = () => useAppStore.setState((s) => ({ sidebarCollapsed: !s.sidebarCollapsed }));
    window.addEventListener("keydown", rerenderOnKey);
    try {
      const onClosed = vi.fn();
      mount(<WizardPage onClosed={onClosed} />);
      await userEvent.click(screen.getByRole("button", { name: "Open" }));

      await userEvent.keyboard("{Escape}");
      await browser.settled();

      expect(onClosed).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener("keydown", rerenderOnKey);
    }
  });
});

describe("the global Esc shortcut stays quiet while the palette is closed", () => {
  it("does not notify store subscribers (no idle re-render of the page)", async () => {
    mount(<div />);
    const listener = vi.fn();
    const unsubscribe = useAppStore.subscribe(listener);
    try {
      await userEvent.keyboard("{Escape}");
      await browser.settled();
      useAppStore.getState().setCommandPaletteOpen(false);
      expect(listener).not.toHaveBeenCalled();
    } finally {
      unsubscribe();
    }
  });
});

describe("dialog close crosses are thumb-sized on touch", () => {
  it("both close buttons carry the touch hit area class", async () => {
    mount(<><WizardPage onClosed={() => {}} /></>);
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(screen.getByRole("button", { name: "Close wizard" })).toHaveClass("touch-hit");
  });

  it("the references dialog close button carries it too", async () => {
    mount(<ReferencesPage onClosed={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    const dialog = screen.getByRole("dialog", { name: /References for Demo project/ });
    const close = Array.from(dialog.querySelectorAll("button")).find((b) => b.getAttribute("aria-label") === "Close");
    expect(close).toHaveClass("touch-hit");
  });

  it("the class makes a 44px target only for a coarse pointer (desktop unchanged)", () => {
    // jsdom cannot evaluate media queries — check the rule itself.
    const css = readFileSync(path.resolve(__dirname, "../../styles/globals.css"), "utf8");
    const rule = css.match(/@media \(pointer: coarse\) \{\s*\.touch-hit \{([^}]*)\}/);
    expect(rule, "touch-hit must live inside @media (pointer: coarse)").not.toBeNull();
    expect(rule![1]).toMatch(/min-width:\s*44px/);
    expect(rule![1]).toMatch(/min-height:\s*44px/);
    // negative margin keeps the header layout unchanged
    expect(rule![1]).toMatch(/margin:\s*calc\(\(var\(--touch-hit-icon, 16px\) - 44px\) \/ 2\)/);
  });
});

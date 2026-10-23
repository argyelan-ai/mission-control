import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

const api = vi.hoisted(() => ({
  open: vi.fn(),
  viewUrl: vi.fn(),
  directory: vi.fn(),
  filesOpen: vi.fn(),
}));
vi.mock("@/lib/api", () => ({
  getToken: () => "tok",
  api: {
    tasks: { deliverables: { open: api.open, viewUrl: api.viewUrl, directory: api.directory } },
    files: { open: api.filesOpen },
  },
}));

import { DeliverableCard } from "../DeliverableCard";
import { HtmlPreview } from "../HtmlPreview";
import { PREVIEW_GUARD_SCRIPT } from "../previewGuard";
import { FinderButton } from "@/components/files/FilePreviewPanel";
import { isLoopbackHostname } from "@/lib/hostOnly";
import type { TaskDeliverable } from "@/lib/types";
import en from "../../../../messages/en.json";
import de from "../../../../messages/de.json";

// Round-2 user tests (2026-10-23), fix package Q4 "Lieferungen":
//   t3 S1 / r1 S4 / t4 / r2 N6 — "Open on the Mac" / "Show in Finder" were
//            decided by CSS alone (wide screen + mouse): a laptop over
//            Tailscale saw them, and a click opened the file unseen on the
//            Mac mini.
//   t3 S1+ — the backend swallowed a failing Mac helper and the card said
//            "Opened on the Mac."
//   t3 N2  — a deliverable whose file is gone (404) kept the Mac buttons.
//   t3 N1  — the cookie stand-in only came when READING the cookie threw;
//            WebKit does not throw there, it silently drops every write.
//   r1 NICE-11 — the page-error hint outlived a change of the frame's page.
//   t3 N3  — say before the start that the page keeps nothing past a reload.

const REMOTE_URL = "https://mc.example.ts.net/tasks";
const LOCAL_URL = "http://localhost:3000/";

/** vitest's jsdom environment exposes its JSDOM instance. */
function setPageUrl(url: string) {
  (globalThis as unknown as { jsdom: { reconfigure: (o: { url: string }) => void } }).jsdom.reconfigure({ url });
}

function mkDeliverable(overrides: Partial<TaskDeliverable> = {}): TaskDeliverable {
  return {
    id: "d1",
    task_id: "t1",
    agent_id: "a1",
    agent_name: "alpha",
    deliverable_type: "file",
    title: "Snake Game",
    path: "/deliverables/c0db2fb6-3ec7-41b7-83a8-48dec2acc499/snake.html",
    description: null,
    content: null,
    created_at: "2026-10-22T10:00:00Z",
    ...overrides,
  } as TaskDeliverable;
}

function withQuery(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

const expand = () => fireEvent.click(screen.getByText("Snake Game"));

beforeEach(() => {
  api.open.mockReset();
  api.viewUrl.mockReset();
  api.directory.mockReset();
  api.filesOpen.mockReset();
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("<!doctype html><p>x</p>", { status: 200 }));
});
afterEach(() => {
  vi.restoreAllMocks();
  setPageUrl(LOCAL_URL);
});

// ── t3 S1: only at the Mac itself ──────────────────────────────────────────

describe("isLoopbackHostname — the same rule as the backend", () => {
  it.each(["localhost", "LOCALHOST", "app.localhost", "127.0.0.1", "127.1.2.3", "::1", "[::1]", "localhost."])(
    "%s is this computer",
    (h) => expect(isLoopbackHostname(h)).toBe(true),
  );
  it.each([
    "mc.example.ts.net",
    "203.0.113.20",
    "198.51.100.7",
    "mac-mini.local",
    "localhost.example.com",
    "127.0.0.1.example.com",
    "",
  ])("%s is another device", (h) => expect(isLoopbackHostname(h)).toBe(false));
});

describe("Mac buttons from another computer (Tailscale / LAN) — t3 S1", () => {
  beforeEach(() => setPageUrl(REMOTE_URL));

  it("the deliverable card offers no Mac action at all", async () => {
    render(<DeliverableCard deliverable={mkDeliverable()} boardId="b" taskId="t" />);
    expand();
    expect(window.location.hostname).toBe("mc.example.ts.net");
    expect(screen.queryByTestId("deliverable-host-actions")).toBeNull();
    expect(screen.queryByRole("button", { name: "Open on the Mac" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Show in Finder" })).toBeNull();
    // The download stays: that is the way to get the file on this device.
    expect(await screen.findByRole("button", { name: /Download/ })).toBeInTheDocument();
  });

  it("a folder deliverable shows no Finder links", async () => {
    api.directory.mockResolvedValue({ root_path: "/deliverables/x/site", entries: [] });
    render(withQuery(<DeliverableCard deliverable={mkDeliverable({ path: "/deliverables/x/site" })} boardId="b" taskId="t" />));
    expand();
    expect(await screen.findByText("The folder is empty.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show folder in Finder" })).toBeNull();
  });

  it("the Files page shows no 'Reveal in Finder'", () => {
    render(<FinderButton root={{ key: "deliverables", label: "Deliverables" } as never} subpath="a.txt" />);
    expect(screen.queryByRole("button", { name: en.files.revealInFinder })).toBeNull();
  });
});

describe("Mac buttons at the Mac itself (localhost)", () => {
  it("the card offers both", () => {
    render(<DeliverableCard deliverable={mkDeliverable()} boardId="b" taskId="t" />);
    expand();
    expect(screen.getByRole("button", { name: "Open on the Mac" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show in Finder" })).toBeInTheDocument();
  });

  it("the Files page offers 'Reveal in Finder'", () => {
    render(<FinderButton root={{ key: "deliverables", label: "Deliverables" } as never} subpath="a.txt" />);
    expect(screen.getByRole("button", { name: en.files.revealInFinder })).toBeInTheDocument();
  });
});

// ── t3 S1 addition: an honest answer ───────────────────────────────────────

describe("the card reports what really happened on the Mac (t3 S1)", () => {
  it("a failing Mac helper (502) is an error, never 'opened'; the buttons stay for another try", async () => {
    api.open.mockRejectedValue(new Error('API 502: {"detail":"helper"}'));
    render(<DeliverableCard deliverable={mkDeliverable()} boardId="b" taskId="t" />);
    expand();
    fireEvent.click(screen.getByRole("button", { name: "Open on the Mac" }));
    expect(await screen.findByText(en.deliverableCard.hostFailed)).toBeInTheDocument();
    expect(screen.queryByText("Opened on the Mac.")).toBeNull();
    expect(screen.getByRole("button", { name: "Open on the Mac" })).toBeInTheDocument();
  });

  it("a refusal (403: not at the Mac) drops the buttons and says so", async () => {
    api.open.mockRejectedValue(new Error('API 403: {"detail":"host only"}'));
    render(<DeliverableCard deliverable={mkDeliverable()} boardId="b" taskId="t" />);
    expand();
    fireEvent.click(screen.getByRole("button", { name: "Show in Finder" }));
    expect(await screen.findByText(/Not possible here/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open on the Mac" })).toBeNull();
  });
});

// ── t3 N2: no Mac buttons for a file that is not there ─────────────────────

describe("a deliverable whose file cannot be loaded (t3 N2)", () => {
  it("file 404: the preview says so and the Mac buttons go", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue(new Response("File not found on disk", { status: 404 }));
    render(<DeliverableCard deliverable={mkDeliverable()} boardId="b" taskId="t" />);
    expand();
    expect(await screen.findByText(en.filePreview.loadFailed)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId("deliverable-host-actions")).toBeNull());
    expect(screen.queryByRole("button", { name: "Open on the Mac" })).toBeNull();
  });

  it("folder 404: the same", async () => {
    api.directory.mockRejectedValue(new Error("API 404: gone"));
    render(withQuery(<DeliverableCard deliverable={mkDeliverable({ path: "/deliverables/x/site" })} boardId="b" taskId="t" />));
    expand();
    expect(await screen.findByText(en.deliverableCard.folderFailed)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId("deliverable-host-actions")).toBeNull());
  });

  it("a file that loads keeps them", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue(new Response("# hi", { status: 200 }));
    render(<DeliverableCard deliverable={mkDeliverable({ path: "/deliverables/x/notes.md" })} boardId="b" taskId="t" />);
    fireEvent.click(screen.getByText("Snake Game"));
    expect(await screen.findByText("hi")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open on the Mac" })).toBeInTheDocument();
  });
});

// ── t3 N1: the cookie stand-in in WebKit ───────────────────────────────────

describe("preview guard — cookie stand-in where reading does not throw (t3 N1, WebKit)", () => {
  /** WebKit's opaque origin: reading gives "", writing is dropped silently. */
  function webkitDocument() {
    const d: Record<string, unknown> = {};
    Object.defineProperty(d, "cookie", { configurable: true, get: () => "", set: () => undefined });
    return d;
  }
  const w = () => ({
    localStorage: { getItem: () => null },
    sessionStorage: { getItem: () => null },
    addEventListener: () => undefined,
    parent: {},
  });

  it("keeps what the page writes, in memory", () => {
    const d = webkitDocument();
    new Function("window", "document", PREVIEW_GUARD_SCRIPT)(w(), d);
    d.cookie = "usertest=1";
    expect(d.cookie).toBe("usertest=1");
  });
});

// ── r1 NICE-11: the hint belongs to the page that reported it ──────────────

describe("HtmlPreview — the page-error hint goes with its page (r1 NICE-11)", () => {
  const MULTI = '<!doctype html><html><body><script src="app.js"></script></body></html>';
  const VIEW = "/api/v1/deliverable-view/dv1.claims.sig/index.html";
  const HINT = /The page reported an error/;

  it("is gone when the frame switches to another source", async () => {
    const mint = vi.fn().mockResolvedValue(VIEW);
    const { rerender } = render(<HtmlPreview html={MULTI} name="index.html" code={<pre />} folderUrl={mint} />);
    fireEvent.click(screen.getByRole("button", { name: "Run page" }));
    const first = (await screen.findByTitle(/Rendered page/i)) as HTMLIFrameElement;
    await waitFor(() => expect(first.getAttribute("src")).toBe(VIEW));
    act(() => {
      window.dispatchEvent(new MessageEvent("message", { data: { mcPreview: "page-error" }, source: first.contentWindow }));
    });
    expect(screen.getByText(HINT)).toBeInTheDocument();

    // Same page, now without a folder URL: the frame is rebuilt from srcdoc.
    rerender(<HtmlPreview html={MULTI} name="index.html" code={<pre />} />);
    await waitFor(() => expect(document.querySelector("iframe")?.hasAttribute("srcdoc")).toBe(true));
    expect(screen.queryByText(HINT)).toBeNull();
  });
});

// ── t3 N3: the start hint says the page keeps nothing ──────────────────────

describe("the start hint says that nothing is kept (t3 N3)", () => {
  it("in English and German", () => {
    expect(en.filePreview.html.runHint).toMatch(/until you reload/);
    expect(de.filePreview.html.runHint).toMatch(/bis zum Neuladen/);
  });

  it("new strings exist in both languages", () => {
    expect(en.deliverableCard.hostFailed).toBeTruthy();
    expect(de.deliverableCard.hostFailed).toMatch(/Mac/);
  });
});

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { render, screen, fireEvent, act, waitFor, within } from "@testing-library/react";
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
import { DirectoryBrowser } from "../DirectoryBrowser";
import { FilePreview } from "../FilePreview";
import { HtmlPreview, buildPreviewDocument, HTML_PREVIEW_CSP } from "../HtmlPreview";
import { PREVIEW_GUARD_SCRIPT } from "../previewGuard";
import { FinderButton } from "@/components/files/FilePreviewPanel";
import { HOST_ONLY_CLASS } from "@/lib/hostOnly";
import type { TaskDeliverable } from "@/lib/types";
import en from "../../../../messages/en.json";
import de from "../../../../messages/de.json";

// Round-1 user tests (2026-10-23), cluster P5 "Lieferungen":
//   t4 M1  — a page that reads localStorage died in the preview (SecurityError
//            in the sandbox's opaque origin, START never bound).
//   r2 S6  — a 12-px "Open with default app" icon right next to the expand
//            arrow opened the file on the MC host, from a phone, silently,
//            in English. t4 S7: "Reveal in Finder" on the phone.
//   r2 S6 / t4 N4 — FILE / DOCUMENT / content for one thing, 9-px chip,
//            path cut without an ellipsis.
//   t4 N1  — the code view drew ligatures ("<!--" as an arrow).

const REPO = join(__dirname, "../../../../..");

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

beforeEach(() => {
  api.open.mockReset();
  api.viewUrl.mockReset();
  api.directory.mockReset();
  api.filesOpen.mockReset();
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("<!doctype html><p>x</p>", { status: 200 }));
});
afterEach(() => vi.restoreAllMocks());

// ── t4 M1: the preview guard ───────────────────────────────────────────────

/** A window like the sandbox's opaque origin: the storages and the cookie
 *  throw a SecurityError on access. */
function opaqueWindow() {
  const listeners: Record<string, () => void> = {};
  const posted: unknown[][] = [];
  const insecure = () => {
    throw new DOMException("The operation is insecure.", "SecurityError");
  };
  const w: Record<string, unknown> = {
    addEventListener: (type: string, fn: () => void) => { listeners[type] = fn; },
    parent: { postMessage: (...args: unknown[]) => posted.push(args) },
  };
  for (const name of ["localStorage", "sessionStorage"]) {
    Object.defineProperty(w, name, { configurable: true, enumerable: true, get: insecure });
  }
  const d: Record<string, unknown> = {};
  Object.defineProperty(d, "cookie", { configurable: true, get: insecure, set: insecure });
  return { w, d, listeners, posted };
}

function runGuard(w: unknown, d: unknown) {
  new Function("window", "document", PREVIEW_GUARD_SCRIPT)(w, d);
}

type MemStorage = Storage & Record<string, unknown>;

describe("preview guard — storage stand-ins for the sandboxed page (t4 M1)", () => {
  it("is the same script the backend puts in front of folder pages", () => {
    const backend = readFileSync(join(REPO, "backend/app/routers/preview_guard.js"), "utf-8");
    expect(PREVIEW_GUARD_SCRIPT).toBe(backend);
  });

  it("never closes its own <script> element early", () => {
    expect(PREVIEW_GUARD_SCRIPT.toLowerCase()).not.toContain("</script");
  });

  it("is part of every srcdoc guard head: after the CSP, before the page", () => {
    const out = buildPreviewDocument("<!doctype html><p>page</p>");
    const csp = `<meta http-equiv="Content-Security-Policy" content="${HTML_PREVIEW_CSP}">`;
    const guard = `<script>${PREVIEW_GUARD_SCRIPT}</script>`;
    expect(out.indexOf(guard)).toBe(out.indexOf(csp) + csp.length);
    expect(out.endsWith(guard + "<!doctype html><p>page</p>")).toBe(true);
  });

  it("gives a page whose storage throws a working in-memory localStorage", () => {
    const { w, d } = opaqueWindow();
    runGuard(w, d);
    const ls = w.localStorage as MemStorage;

    // snake.html:199 — the first read of the high score.
    expect(ls.getItem("snake-high")).toBeNull();
    ls.setItem("snake-high", 12 as unknown as string);
    expect(ls.getItem("snake-high")).toBe("12");
    // Property syntax, as many pages write it.
    ls.level = 3;
    expect(ls.getItem("level")).toBe("3");
    expect(ls["snake-high"]).toBe("12");
    expect(ls.length).toBe(2);
    expect(Object.keys(ls)).toEqual(["snake-high", "level"]);
    expect(ls.key(0)).toBe("snake-high");
    expect("level" in ls).toBe(true);
    ls.removeItem("level");
    expect(ls.getItem("level")).toBeNull();
    ls.clear();
    expect(ls.length).toBe(0);
  });

  it("keeps sessionStorage apart from localStorage", () => {
    const { w, d } = opaqueWindow();
    runGuard(w, d);
    (w.sessionStorage as Storage).setItem("k", "s");
    expect((w.localStorage as Storage).getItem("k")).toBeNull();
    expect((w.sessionStorage as Storage).getItem("k")).toBe("s");
  });

  it("gives document.cookie a jar in memory", () => {
    const { w, d } = opaqueWindow();
    runGuard(w, d);
    d.cookie = "a=1; path=/";
    d.cookie = "b=2";
    expect(d.cookie).toBe("a=1; b=2");
    d.cookie = "a=; max-age=0";
    expect(d.cookie).toBe("b=2");
  });

  it("leaves a storage that works alone", () => {
    const real = { getItem: () => "real" };
    const w = { localStorage: real, sessionStorage: real, addEventListener: () => undefined, parent: {} };
    const d = { cookie: "" };
    runGuard(w, d);
    expect(w.localStorage).toBe(real);
  });

  it("tells MC once, with a fixed word, when the page still throws", () => {
    const { w, d, listeners, posted } = opaqueWindow();
    runGuard(w, d);
    listeners.error();
    listeners.unhandledrejection();
    expect(posted).toEqual([[{ mcPreview: "page-error" }, "*"]]);
  });
});

describe("HtmlPreview — a visible hint when the page throws (t4 M1)", () => {
  function renderRunning() {
    render(<HtmlPreview html="<p>x</p>" name="snake.html" code={<pre />} />);
    fireEvent.click(screen.getByRole("button", { name: "Run page" }));
    return document.querySelector("iframe") as HTMLIFrameElement;
  }
  const HINT = /The page reported an error/;

  it("shows the hint outside the frame when the running page reports an error", () => {
    const frame = renderRunning();
    expect(screen.queryByText(HINT)).toBeNull();
    act(() => {
      window.dispatchEvent(new MessageEvent("message", { data: { mcPreview: "page-error" }, source: frame.contentWindow }));
    });
    const hint = screen.getByText(HINT);
    expect(frame.contains(hint)).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss hint" }));
    expect(screen.queryByText(HINT)).toBeNull();
  });

  it("ignores the same message from anyone but this frame, and other messages from it", () => {
    const frame = renderRunning();
    act(() => {
      window.dispatchEvent(new MessageEvent("message", { data: { mcPreview: "page-error" }, source: window }));
      window.dispatchEvent(new MessageEvent("message", { data: { mcPreview: "other" }, source: frame.contentWindow }));
      window.dispatchEvent(new MessageEvent("message", { data: "page-error", source: frame.contentWindow }));
    });
    expect(screen.queryByText(HINT)).toBeNull();
  });
});

// ── t4 N1: no ligatures in code ────────────────────────────────────────────

describe("code view — no font ligatures (t4 N1)", () => {
  it("turns ligatures off, so '<!--' and '-->' stay as typed", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue(new Response("<!-- c --> a => b", { status: 200 }));
    render(<FilePreview fileUrl="/f" path="x.css" />);
    await waitFor(() => expect(document.querySelector("pre")).not.toBeNull());
    expect(document.querySelector("pre")?.getAttribute("style")).toContain("font-variant-ligatures: none");
  });
});

// ── r2 S6 / t4 S7 / N4: the deliverable card ───────────────────────────────

describe("DeliverableCard — kind, sizes, path (r2 S6, t4 N4)", () => {
  it.each([
    [{ path: "/deliverables/x/snake.html" }, "HTML"],
    [{ path: "/deliverables/x/report.pdf" }, "PDF"],
    [{ path: "/deliverables/x/site" }, "Folder"],
    [{ deliverable_type: "url", path: "https://example.org" }, "Link"],
    [{ deliverable_type: "document", path: null, content: "# Notes" }, "Text"],
  ] as [Partial<TaskDeliverable>, string][])("names the kind by what it is: %o → %s", (over, kind) => {
    render(<DeliverableCard deliverable={mkDeliverable(over)} boardId="b" taskId="t" />);
    expect(screen.getByTestId("deliverable-kind")).toHaveTextContent(kind);
    // Not the internal type words, and no second "content" badge.
    expect(screen.queryByText(/^(file|document|url|content)$/i)).toBeNull();
  });

  it("uses no text below 11 px", () => {
    const { container } = render(
      <DeliverableCard deliverable={mkDeliverable({ source_depth: 1, source_task_title: "Sub" })} boardId="b" taskId="t" />,
    );
    expect(container.querySelectorAll('[class*="text-[9px]"], [class*="text-[10px]"]')).toHaveLength(0);
  });

  it("wraps the path instead of cutting it, and the path does nothing on a tap", () => {
    render(<DeliverableCard deliverable={mkDeliverable()} boardId="b" taskId="t" />);
    const path = screen.getByText("/deliverables/c0db2fb6-3ec7-41b7-83a8-48dec2acc499/snake.html");
    expect(path).toHaveClass("break-all");
    expect(path).not.toHaveClass("truncate");
    expect(path.closest("button")).toBeNull();
  });
});

describe("DeliverableCard — the Mac actions (r2 S6, t4 S7)", () => {
  it("keeps the collapsed header free of anything that acts on the Mac", () => {
    render(<DeliverableCard deliverable={mkDeliverable()} boardId="b" taskId="t" />);
    expect(screen.queryByRole("button", { name: /mac|finder|default app/i })).toBeNull();
    fireEvent.click(screen.getByText("Snake Game"));
    expect(api.open).not.toHaveBeenCalled();
  });

  it("offers them, in words, only when expanded and only at a desktop with a mouse", () => {
    render(<DeliverableCard deliverable={mkDeliverable()} boardId="b" taskId="t" />);
    fireEvent.click(screen.getByText("Snake Game"));
    const row = screen.getByTestId("deliverable-host-actions");
    expect(row.className).toContain(HOST_ONLY_CLASS);
    expect(within(row).getByRole("button", { name: "Open on the Mac" })).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Show in Finder" })).toBeInTheDocument();
  });

  it("says what happened after a click", async () => {
    api.open.mockResolvedValue({ ok: true });
    render(<DeliverableCard deliverable={mkDeliverable()} boardId="b" taskId="t" />);
    fireEvent.click(screen.getByText("Snake Game"));
    fireEvent.click(screen.getByRole("button", { name: "Open on the Mac" }));
    expect(api.open).toHaveBeenCalledWith("b", "t", "d1", { reveal: false });
    expect(await screen.findByText("Opened on the Mac.")).toBeInTheDocument();
  });

  it("says when it is not possible here and drops the buttons", async () => {
    api.open.mockRejectedValue(new Error("501"));
    render(<DeliverableCard deliverable={mkDeliverable()} boardId="b" taskId="t" />);
    fireEvent.click(screen.getByText("Snake Game"));
    fireEvent.click(screen.getByRole("button", { name: "Show in Finder" }));
    expect(api.open).toHaveBeenCalledWith("b", "t", "d1", { reveal: true, subpath: undefined });
    expect(await screen.findByText(/Not possible here/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open on the Mac" })).toBeNull();
  });
});

describe("Finder buttons elsewhere — desktop only, in words (t4 S7)", () => {
  it("Files page: 'Reveal in Finder' is hidden on a phone and comes from i18n", () => {
    render(<FinderButton root={{ key: "deliverables", label: "Deliverables" } as never} subpath="a.txt" />);
    const btn = screen.getByRole("button", { name: en.files.revealInFinder });
    expect(btn.className).toContain(HOST_ONLY_CLASS);
  });

  it("Folder deliverables: the Finder links are desktop only and from i18n", async () => {
    api.directory.mockResolvedValue({ root_path: "/deliverables/x/site", entries: [] });
    render(withQuery(<DirectoryBrowser boardId="b" taskId="t" deliverableId="d" onOpenInFinder={() => undefined} />));
    const btn = await screen.findByRole("button", { name: "Show folder in Finder" });
    expect(btn.className).toContain(HOST_ONLY_CLASS);
    expect(screen.getByText("The folder is empty.")).toBeInTheDocument();
  });

  it("the shared class hides on phones and shows from md with a fine pointer", () => {
    expect(HOST_ONLY_CLASS.split(" ")).toEqual(["hidden", "md:pointer-fine:inline-flex"]);
  });
});

describe("deliverableCard i18n", () => {
  type Tree = { [k: string]: string | Tree };
  const flat = (t: Tree, p = ""): string[] =>
    Object.entries(t).flatMap(([k, v]) => (typeof v === "string" ? [p + k] : flat(v, `${p}${k}.`)));

  it("has the same keys in English and German", () => {
    const enNs = (en as unknown as Record<string, Tree>).deliverableCard;
    const deNs = (de as unknown as Record<string, Tree>).deliverableCard;
    expect(enNs).toBeDefined();
    expect(flat(deNs).sort()).toEqual(flat(enNs).sort());
  });

  it("speaks German in the German catalog", () => {
    const ns = (de as unknown as { deliverableCard: Record<string, string | Record<string, string>> }).deliverableCard;
    expect(ns.openOnMac).toBe("Auf dem Mac öffnen");
    expect(ns.showInFinder).toBe("Im Finder zeigen");
    expect((ns.kind as Record<string, string>).folder).toBe("Ordner");
    const html = (de as unknown as { filePreview: { html: Record<string, string> } }).filePreview.html;
    expect(html.pageError).toMatch(/^Die Seite hat einen Fehler gemeldet/);
  });
});

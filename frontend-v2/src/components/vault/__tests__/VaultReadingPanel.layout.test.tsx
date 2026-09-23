/**
 * /memory reading panel — the note body must get the room.
 *
 * The masthead (title, meta, tags) and the "Related" box sat fixed above an
 * inner scroll area; with 12 related notes the body was left ~89 px at
 * 1440×900. Now the whole panel scrolls as one, and Related starts folded to
 * three entries with a "Show N more" toggle.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { VaultNote } from "@/lib/types";

const related: VaultNote[] = Array.from({ length: 13 }, (_, i) => ({
  path: `tasks/t1/note-${i}.md`,
  id: `n${i}`,
  agent: "alpha",
  type: "journal",
  tags: "",
  project: "",
  title: `Related ${i}`,
  date: "",
  content: "",
}));

vi.mock("@/lib/api", () => ({
  api: { vault: { related: vi.fn(async () => ({ notes: related })) } },
}));
vi.mock("@/hooks/useVaultNote", () => ({
  useVaultNote: () => ({
    data: { frontmatter: { title: "Run record", task: "task-1" }, body: "Body text" },
    isLoading: false,
    isError: false,
  }),
}));
vi.mock("../VaultMarkdown", () => ({ VaultMarkdown: () => <div data-testid="note-body">Body text</div> }));
vi.mock("../AttachmentPreview", () => ({ AttachmentPreview: () => null }));

import { VaultReadingPanel } from "../VaultReadingPanel";

const note: VaultNote = { ...related[0], path: "tasks/t1/current.md", title: "Run record" };

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <VaultReadingPanel note={note} onClose={vi.fn()} onWikilinkClick={vi.fn()} onSelectNote={vi.fn()} />
    </QueryClientProvider>,
  );
}

function scrollParent(el: HTMLElement): HTMLElement | null {
  let cur: HTMLElement | null = el.parentElement;
  while (cur && !/\boverflow-y-auto\b/.test(cur.className)) cur = cur.parentElement;
  return cur;
}

describe("VaultReadingPanel layout", () => {
  it("scrolls the masthead, Related and the body in one container", async () => {
    renderPanel();
    const relatedLabel = await screen.findByText(/related · 13/i);
    const body = screen.getByTestId("note-body");
    const heading = screen.getAllByText("Run record")[0];
    const frame = scrollParent(body);
    expect(frame).not.toBeNull();
    expect(scrollParent(heading)).toBe(frame);
    expect(scrollParent(relatedLabel)).toBe(frame);
  });

  it("folds Related to three entries and expands on demand", async () => {
    renderPanel();
    await screen.findByText(/related · 13/i);
    expect(screen.getByText("Related 0")).toBeInTheDocument();
    expect(screen.getByText("Related 2")).toBeInTheDocument();
    expect(screen.queryByText("Related 3")).toBeNull();
    const toggle = screen.getByRole("button", { name: "Show 10 more" });
    // Shared CappedList control: 44 px touch target on phones, shared wording.
    expect(toggle.className).toMatch(/\bmin-h-11\b/);
    fireEvent.click(toggle);
    expect(screen.getByText("Related 12")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show fewer" })).toBeInTheDocument();
  });
});

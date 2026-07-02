import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { FilesBrowser, sortEntries } from "../FilesBrowser";
import type { FsEntry, FsRoot } from "@/lib/types";
import { api } from "@/lib/api";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const ROOT: FsRoot = {
  key: "vault",
  label: "Vault",
  icon: "BookOpen",
  native_open: true,
  indexed_count: 12,
  deletable: true,
};

// Default multi-select props — most tests don't care about selection.
const noSel = {
  selected: new Set<string>(),
  onToggleSelect: () => {},
  onToggleSelectAll: () => {},
};

const mkEntry = (o: Partial<FsEntry>): FsEntry => ({
  name: "x",
  type: "file",
  size: 1024,
  mime: null,
  mtime: 1_700_000_000,
  is_directory: false,
  ...o,
});

describe("FilesBrowser", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders entries from api.files.list (folders first)", async () => {
    vi.spyOn(api.files, "list").mockResolvedValue({
      root: "vault",
      subpath: "",
      entries: [
        mkEntry({ name: "notes.md" }),
        mkEntry({ name: "subdir", type: "directory", is_directory: true }),
      ],
    });

    renderWithQuery(
      <FilesBrowser root={ROOT} subpath="" onNavigate={() => {}} onSelectFile={() => {}} {...noSel} />
    );

    expect(await screen.findByText("notes.md")).toBeInTheDocument();
    expect(screen.getByText("subdir")).toBeInTheDocument();
    // Breadcrumb shows the root label.
    expect(screen.getByRole("button", { name: "Vault" })).toBeInTheDocument();
  });

  it("navigates on folder click (calls onNavigate with the folder subpath)", async () => {
    vi.spyOn(api.files, "list").mockResolvedValue({
      root: "vault",
      subpath: "",
      entries: [mkEntry({ name: "subdir", type: "directory", is_directory: true })],
    });
    const onNavigate = vi.fn();

    renderWithQuery(
      <FilesBrowser root={ROOT} subpath="" onNavigate={onNavigate} onSelectFile={() => {}} {...noSel} />
    );

    await userEvent.click(await screen.findByText("subdir"));
    await waitFor(() => expect(onNavigate).toHaveBeenCalledWith("subdir"));
  });

  it("selects a file on click (calls onSelectFile, not onNavigate)", async () => {
    vi.spyOn(api.files, "list").mockResolvedValue({
      root: "vault",
      subpath: "docs",
      entries: [mkEntry({ name: "readme.md" })],
    });
    const onNavigate = vi.fn();
    const onSelectFile = vi.fn();

    renderWithQuery(
      <FilesBrowser root={ROOT} subpath="docs" onNavigate={onNavigate} onSelectFile={onSelectFile} {...noSel} />
    );

    await userEvent.click(await screen.findByText("readme.md"));
    // Subpath is built relative to the active directory.
    await waitFor(() => expect(onSelectFile).toHaveBeenCalledWith("docs/readme.md"));
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it("toggles sort direction on header click (aria-sort)", async () => {
    vi.spyOn(api.files, "list").mockResolvedValue({
      root: "vault", subpath: "",
      entries: [mkEntry({ name: "a.txt" }), mkEntry({ name: "b.txt" })],
    });
    renderWithQuery(
      <FilesBrowser root={ROOT} subpath="" onNavigate={() => {}} onSelectFile={() => {}} {...noSel} />
    );
    await screen.findByText("a.txt");
    const sizeHeader = screen.getByText("Grösse").closest("th")!;
    expect(sizeHeader).toHaveAttribute("aria-sort", "none");
    await userEvent.click(screen.getByText("Grösse"));
    expect(sizeHeader).toHaveAttribute("aria-sort", "ascending");
    await userEvent.click(screen.getByText("Grösse"));
    expect(sizeHeader).toHaveAttribute("aria-sort", "descending");
  });

  // ── Multi-select ──────────────────────────────────────────────────────────

  it("renders one checkbox per file, none per folder", async () => {
    vi.spyOn(api.files, "list").mockResolvedValue({
      root: "vault", subpath: "",
      entries: [
        mkEntry({ name: "a.txt" }),
        mkEntry({ name: "b.txt" }),
        mkEntry({ name: "dir", type: "directory", is_directory: true }),
      ],
    });
    renderWithQuery(
      <FilesBrowser root={ROOT} subpath="" onNavigate={() => {}} onSelectFile={() => {}} {...noSel} />
    );
    await screen.findByText("a.txt");
    // Two files → two per-file checkboxes; folder has none.
    expect(screen.getByRole("checkbox", { name: "a.txt auswählen" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "b.txt auswählen" })).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "dir auswählen" })).not.toBeInTheDocument();
  });

  it("toggling a file checkbox calls onToggleSelect(subpath, true)", async () => {
    vi.spyOn(api.files, "list").mockResolvedValue({
      root: "vault", subpath: "docs",
      entries: [mkEntry({ name: "readme.md" })],
    });
    const onToggleSelect = vi.fn();
    renderWithQuery(
      <FilesBrowser
        root={ROOT} subpath="docs" onNavigate={() => {}} onSelectFile={() => {}}
        selected={new Set()} onToggleSelect={onToggleSelect} onToggleSelectAll={() => {}}
      />
    );
    await screen.findByText("readme.md");
    await userEvent.click(screen.getByRole("checkbox", { name: "readme.md auswählen" }));
    expect(onToggleSelect).toHaveBeenCalledWith("docs/readme.md", true);
  });

  it("header select-all calls onToggleSelectAll with every file subpath", async () => {
    vi.spyOn(api.files, "list").mockResolvedValue({
      root: "vault", subpath: "",
      entries: [
        mkEntry({ name: "a.txt" }),
        mkEntry({ name: "b.txt" }),
        mkEntry({ name: "dir", type: "directory", is_directory: true }),
      ],
    });
    const onToggleSelectAll = vi.fn();
    renderWithQuery(
      <FilesBrowser
        root={ROOT} subpath="" onNavigate={() => {}} onSelectFile={() => {}}
        selected={new Set()} onToggleSelect={() => {}} onToggleSelectAll={onToggleSelectAll}
      />
    );
    await screen.findByText("a.txt");
    await userEvent.click(screen.getByRole("checkbox", { name: "Alle Dateien auswählen" }));
    expect(onToggleSelectAll).toHaveBeenCalledWith(["a.txt", "b.txt"], true);
  });

  it("header checkbox is indeterminate when only some files are selected", async () => {
    vi.spyOn(api.files, "list").mockResolvedValue({
      root: "vault", subpath: "",
      entries: [mkEntry({ name: "a.txt" }), mkEntry({ name: "b.txt" })],
    });
    renderWithQuery(
      <FilesBrowser
        root={ROOT} subpath="" onNavigate={() => {}} onSelectFile={() => {}}
        selected={new Set(["a.txt"])} onToggleSelect={() => {}} onToggleSelectAll={() => {}}
      />
    );
    await screen.findByText("a.txt");
    const header = screen.getByRole("checkbox", { name: "Alle Dateien auswählen" }) as HTMLInputElement;
    expect(header.indeterminate).toBe(true);
    expect(header.checked).toBe(false);
  });

  it("clicking a row checkbox does NOT navigate or open the file", async () => {
    vi.spyOn(api.files, "list").mockResolvedValue({
      root: "vault", subpath: "",
      entries: [mkEntry({ name: "a.txt" })],
    });
    const onNavigate = vi.fn();
    const onSelectFile = vi.fn();
    renderWithQuery(
      <FilesBrowser
        root={ROOT} subpath="" onNavigate={onNavigate} onSelectFile={onSelectFile}
        selected={new Set()} onToggleSelect={() => {}} onToggleSelectAll={() => {}}
      />
    );
    await screen.findByText("a.txt");
    await userEvent.click(screen.getByRole("checkbox", { name: "a.txt auswählen" }));
    expect(onNavigate).not.toHaveBeenCalled();
    expect(onSelectFile).not.toHaveBeenCalled();
  });
});

describe("sortEntries", () => {
  const dir = (name: string) => mkEntry({ name, type: "directory", is_directory: true });
  const file = (name: string, size: number, mtime: number) => mkEntry({ name, size, mtime });

  it("keeps folders above files for every column/direction", () => {
    const items = [file("b.txt", 10, 1), dir("zed"), file("a.txt", 99, 2), dir("alpha")];
    const cases: [("name" | "size" | "mtime"), ("asc" | "desc")][] = [
      ["name", "asc"], ["size", "desc"], ["mtime", "asc"],
    ];
    for (const [k, d] of cases) {
      const sorted = sortEntries(items, k, d);
      const firstFileIdx = sorted.findIndex((e) => !e.is_directory);
      const lastDirIdx = sorted.map((e) => e.is_directory).lastIndexOf(true);
      expect(lastDirIdx).toBeLessThan(firstFileIdx);
    }
  });

  it("sorts by name asc and desc", () => {
    const items = [file("charlie", 1, 1), file("alpha", 1, 1), file("bravo", 1, 1)];
    expect(sortEntries(items, "name", "asc").map((e) => e.name)).toEqual(["alpha", "bravo", "charlie"]);
    expect(sortEntries(items, "name", "desc").map((e) => e.name)).toEqual(["charlie", "bravo", "alpha"]);
  });

  it("sorts files by size", () => {
    const items = [file("big", 9000, 1), file("small", 10, 1), file("mid", 500, 1)];
    expect(sortEntries(items, "size", "asc").map((e) => e.name)).toEqual(["small", "mid", "big"]);
    expect(sortEntries(items, "size", "desc").map((e) => e.name)).toEqual(["big", "mid", "small"]);
  });

  it("sorts files by mtime (modified)", () => {
    const items = [file("old", 1, 100), file("new", 1, 300), file("mid", 1, 200)];
    expect(sortEntries(items, "mtime", "desc").map((e) => e.name)).toEqual(["new", "mid", "old"]);
    expect(sortEntries(items, "mtime", "asc").map((e) => e.name)).toEqual(["old", "mid", "new"]);
  });
});

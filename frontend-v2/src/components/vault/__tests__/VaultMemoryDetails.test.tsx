/**
 * Memory page details from round-3 user test t4 N3–N5: honest counts, no
 * raw Markdown in previews, no keyboard hint on phones, no placeholder agent,
 * no doubled title, no empty image for an HTML attachment, task links that
 * open the task, and dates/labels that follow the UI language.
 */
import { describe, it, expect, vi } from "vitest";
import { render as rtlRender, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import en from "../../../../messages/en.json";
import de from "../../../../messages/de.json";
import { VaultHeader } from "../VaultHeader";
import { VaultMarkdown, stripLeadingTitle } from "../VaultMarkdown";
import {
  excerptFromContent,
  formatLongDate,
  formatMonthLabel,
  parseDateFromNote,
} from "../VaultNoteRow";

// Wikilink chips fetch a hover preview through TanStack Query.
function render(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("VaultHeader", () => {
  it("shows '100+' when the search hit its result cap", () => {
    render(<VaultHeader noteCount={100} noteCountCapped agentCount={3} />);
    expect(screen.getByText(/100\+ notes · 3 agents/i)).toBeInTheDocument();
  });

  it("shows the plain count otherwise, through i18n", () => {
    render(<VaultHeader noteCount={42} agentCount={1} />);
    expect(screen.getByText(/42 notes · 1 agents?/i)).toBeInTheDocument();
  });

  it("hides the ⌘K hint on phones", () => {
    render(<VaultHeader noteCount={1} agentCount={1} />);
    const hint = screen.getByText("⌘K");
    expect(hint.className).toMatch(/\bhidden\b/);
    expect(hint.className).toMatch(/\bmd:block\b/);
  });
});

describe("search placeholder", () => {
  it("does not name an agent that does not exist", () => {
    expect(en.vault.searchPlaceholder).not.toMatch(/alpha/i);
    expect(de.vault.searchPlaceholder).not.toMatch(/alpha/i);
  });
});

describe("excerptFromContent", () => {
  it("drops Markdown syntax, embeds and link targets", () => {
    const content = [
      "---", "title: X", "---",
      "# Title",
      "> Auto-imported from task [[task-2379cb9f-5fde-4bd8-8f02-801a4afb17cc]].",
      "",
      "![[../../../attachments/files/abc.html]]",
      "",
      "**For the operator** see [docs](https://example.com) and `code`.",
      "- item one",
    ].join("\n");
    const out = excerptFromContent(content);
    expect(out).not.toMatch(/[>*`!]|\[\[|\]\]|\]\(|attachments\//);
    expect(out).toContain("Auto-imported from task 2379cb9f.");
    expect(out).toContain("For the operator see docs and code.");
    expect(out).toContain("item one");
  });
});

describe("dates follow the UI language", () => {
  it("month label", () => {
    const d = parseDateFromNote({ path: "a.md", content: "", date: "2026-10-23T08:00:00Z" })!;
    expect(formatMonthLabel(d, "en")).toBe("Oct 2026");
    expect(formatMonthLabel(d, "de")).toBe("Okt. 2026");
  });

  it("long date", () => {
    expect(formatLongDate("2026-10-23T08:00:00Z", "de")).toBe("23. Oktober 2026");
    expect(formatLongDate("2026-10-23T08:00:00Z", "en")).toBe("October 23, 2026");
  });
});

describe("VaultMarkdown", () => {
  it("renders wikilinks as note chips (the URL sanitizer used to blank them)", () => {
    render(<VaultMarkdown content="See [[rate-limits]]." />);
    expect(screen.getByRole("button", { name: /rate-limits/ })).toBeInTheDocument();
  });

  it("[[name|alias]] shows the alias and links to the name", () => {
    const onClick = vi.fn();
    render(<VaultMarkdown content="See [[rate-limits|Rate limits]]." onWikilinkClick={onClick} />);
    const chip = screen.getByRole("button", { name: "Search for “Rate limits”" });
    expect(chip).toHaveTextContent("[Rate limits]");
    fireEvent.click(chip);
    expect(onClick).toHaveBeenCalledWith("rate-limits");
  });

  it("an embed's |size part is not part of the file name", () => {
    render(<VaultMarkdown content={"![[../../attachments/files/shot.png|300]]"} />);
    expect(screen.getByText("shot.png")).toBeInTheDocument();
  });

  it("links [[task-<uuid>]] to the task, not to the note itself", () => {
    const id = "2379cb9f-5fde-4bd8-8f02-801a4afb17cc";
    render(<VaultMarkdown content={`> From task [[task-${id}]].`} />);
    const link = screen.getByRole("link", { name: "Open task 2379cb9f" });
    expect(link).toHaveAttribute("href", `/tasks?task=${id}`);
    expect(link).toHaveTextContent("2379cb9f");
  });

  it("never renders an attachment embed as an (empty) image", () => {
    const { container } = render(
      <VaultMarkdown content={"![[../../attachments/files/page.html]]"} />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("page.html")).toBeInTheDocument();
  });

  it("drops embeds entirely when the attachment preview is shown below", () => {
    const { container } = render(
      <VaultMarkdown content={"Intro\n\n![[../../attachments/files/page.html]]"} hideEmbeds />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(screen.queryByText("page.html")).toBeNull();
    expect(screen.getByText("Intro")).toBeInTheDocument();
  });
});

describe("stripLeadingTitle", () => {
  it("removes a first H1 that repeats the panel title", () => {
    const body = "# Usertest Page (index.html)\n\n> quote";
    expect(stripLeadingTitle(body, "Usertest Page (index.html)")).toBe("> quote");
  });

  it("keeps an H1 that says something else", () => {
    const body = "# Another heading\n\ntext";
    expect(stripLeadingTitle(body, "Title")).toBe(body);
  });
});

describe("i18n catalog", () => {
  it("has the new memory keys in both languages", () => {
    for (const cat of [en, de]) {
      const v = cat.vault as Record<string, unknown>;
      expect(typeof v.headerStats).toBe("string");
      expect(typeof v.taskLink).toBe("string");
      expect(typeof (v.graph as Record<string, string>).building).toBe("string");
      expect(typeof (v.noteType as Record<string, string>).deliverable).toBe("string");
    }
    expect(de.vault.graph.building).not.toBe(en.vault.graph.building);
  });

  it("calls a deliverable note the same as the rest of the app (Datei / File)", () => {
    expect(de.vault.noteType.deliverable).toBe("Datei");
    expect(en.vault.noteType.deliverable).toBe("File");
    expect(de.vault.showAllNotes).not.toMatch(/Notes/);
  });
});

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { FilePreview } from "../FilePreview";

// FilePreview generalization — markdown renders RICH (real heading element,
// not a highlighted code block) and a Download control is always offered,
// including for unsupported types (mobile-correct fallback).

describe("FilePreview", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    const storage = {
      getItem: () => "tok",
      setItem: () => undefined,
      removeItem: () => undefined,
      clear: () => undefined,
    };
    Object.defineProperty(globalThis, "localStorage", {
      value: storage, configurable: true, writable: true,
    });
  });

  afterEach(() => {
    fetchSpy?.mockRestore();
  });

  it("renders markdown rich — a real <h1>, not a code block", async () => {
    fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("# Report Heading\n\nSome **bold** body text.", { status: 200 })
    );

    render(<FilePreview fileUrl="/api/v1/files/content?root=vault&subpath=r.md" path="r.md" />);

    // The heading text appears inside a heading element (rich markdown),
    // NOT inside a <pre>/<code> source block.
    const heading = await screen.findByRole("heading", { name: /Report Heading/i });
    expect(heading.tagName.toLowerCase()).toBe("h1");

    // Guard: the heading is not wrapped in a code/pre element.
    expect(heading.closest("pre")).toBeNull();
    expect(heading.closest("code")).toBeNull();
  });

  it("always shows a Download control for unsupported file types", () => {
    fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("", { status: 200 }));

    render(<FilePreview fileUrl="/api/v1/files/content?root=vault&subpath=archive.zip" path="archive.zip" />);

    // Download button present for an unsupported type…
    expect(screen.getByRole("button", { name: /herunterladen/i })).toBeInTheDocument();
    // …and the user is told there is no inline preview.
    expect(screen.getByText(/Keine Vorschau/i)).toBeInTheDocument();
  });

  it("offers Download for image types too (preview + fallback)", async () => {
    // String-Body statt jsdom-Blob: undicis Response.blob() ruft intern
    // body.stream() auf, das jsdoms Blob nicht implementiert (CI-only-Crash;
    // lokal kaschiert neueres Node die Interop).
    fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("x", { status: 200 })
    );

    render(<FilePreview fileUrl="/api/v1/files/content?root=media&subpath=pic.png" path="pic.png" />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /herunterladen/i })).toBeInTheDocument()
    );
  });
});

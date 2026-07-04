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
    expect(screen.getByRole("button", { name: /download/i })).toBeInTheDocument();
    // …and the user is told there is no inline preview.
    expect(screen.getByText(/No preview/i)).toBeInTheDocument();
  });

  it("offers Download for image types too (preview + fallback)", async () => {
    // String body instead of a jsdom Blob: undici's Response.blob() calls
    // body.stream() internally, which jsdom's Blob doesn't implement
    // (CI-only crash; locally a newer Node version papers over the interop gap).
    fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("x", { status: 200 })
    );

    render(<FilePreview fileUrl="/api/v1/files/content?root=media&subpath=pic.png" path="pic.png" />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /download/i })).toBeInTheDocument()
    );
  });
});

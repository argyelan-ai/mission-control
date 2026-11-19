/**
 * Round-4 user test t4 NICE-2: the attachment card under a delivery note read
 * "text/html · 1 KB · Download Usertest R3 Mini-Seite (index.html)" in a
 * German UI — "Download …" was fixed English text and the type the raw MIME
 * string. Every word on the card now comes from i18n, the type in words.
 */
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { AttachmentPreview, fileKindLabel } from "../AttachmentPreview";

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, blob: async () => new Blob(["<p>x</p>"]) }));
  URL.createObjectURL = vi.fn(() => "blob:x");
  URL.revokeObjectURL = vi.fn();
});
afterEach(() => vi.unstubAllGlobals());

describe("AttachmentPreview — words from i18n", () => {
  it("names the type in words and the download link through i18n", async () => {
    render(<AttachmentPreview deliverableId="d1" mime="text/html" sizeBytes={1100} title="Mini page (index.html)" />);
    const link = await screen.findByRole("link", { name: "Download Mini page (index.html)" });
    expect(link).toHaveAttribute("download");
    expect(screen.getByText("HTML file")).toBeInTheDocument();
    expect(screen.queryByText("text/html")).not.toBeInTheDocument();
  });

  it("a failed load says so in the UI language", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 404 }));
    render(<AttachmentPreview deliverableId="d1" mime="text/html" />);
    expect(await screen.findByText(/Could not load the attachment/)).toBeInTheDocument();
  });
});

describe("fileKindLabel", () => {
  const t = (k: string, v?: Record<string, string>) => (v ? `${k}:${v.ext}` : k);
  it("maps common types, falls back to the subtype", () => {
    expect(fileKindLabel("text/html", t)).toBe("fileKind.html");
    expect(fileKindLabel("application/pdf", t)).toBe("fileKind.pdf");
    expect(fileKindLabel("image/png", t)).toBe("fileKind.image:PNG");
    expect(fileKindLabel("application/zip", t)).toBe("fileKind.other:ZIP");
    expect(fileKindLabel("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", t)).toBe("fileKind.spreadsheet");
  });
});

import { describe, it, expect } from "vitest";
import { createHash } from "node:crypto";
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import nextConfig from "../../next.config";

// next.config.ts serves /fonts/* as `immutable` for a year: a browser that
// has a font never asks for it again. That is only safe while a font file
// never changes under the same name. This list pins every file's content to
// its name. Changed a font? Give the file a NEW name (e.g. GeneralSans-400-v2
// .woff2), update src/styles/globals.css and the preloads in app/layout.tsx,
// then add the new name here — never edit a hash in place.
const FONT_SHA256_PREFIX: Record<string, string> = {
  "ClashDisplay-400.woff2": "6508dfc1a8e9623e",
  "ClashDisplay-500.woff2": "6de911fc824613ef",
  "ClashDisplay-600.woff2": "e748ea11c49f39ae",
  "ClashDisplay-700.woff2": "532795f825c5a28b",
  "GeneralSans-400.woff2": "3ec2be771caf168b",
  "GeneralSans-500.woff2": "c30377df1de8444d",
  "GeneralSans-600.woff2": "94a2a0e1ef59728e",
  "JetBrainsMono-Bold.woff2": "08863c7964612257",
  "JetBrainsMono-Regular.woff2": "f1a7a03672cdd494",
  "SpaceMono-Bold.woff2": "af7cf6d2b897ec45",
  "SpaceMono-Regular.woff2": "e0c8e616bda27642",
  "SymbolsNerdFontMono-Regular.ttf": "f0f624d9b474bea1",
};

const FONT_DIR = path.resolve(__dirname, "../../public/fonts");

describe("bundled fonts are cached as immutable", () => {
  it("every font file keeps the content its name was pinned to", () => {
    const files = readdirSync(FONT_DIR).filter((f) => /\.(woff2?|ttf|otf)$/.test(f));
    const actual = Object.fromEntries(
      files.map((f) => [f, createHash("sha256").update(readFileSync(path.join(FONT_DIR, f))).digest("hex").slice(0, 16)]),
    );
    expect(actual).toEqual(FONT_SHA256_PREFIX);
  });

  it("next.config sends a one-year immutable Cache-Control for /fonts/*", async () => {
    const rules = (await nextConfig.headers?.()) ?? [];
    const fonts = rules.find((r) => r.source.startsWith("/fonts/"));
    expect(fonts?.headers).toContainEqual({
      key: "Cache-Control",
      value: "public, max-age=31536000, immutable",
    });
  });
});

"use client";

/**
 * SVG files (an agent's chart, logo or diagram) are shown as their SOURCE,
 * never drawn by MC — with one line on why, and the download to see the
 * picture outside MC.
 *
 * Why not draw it: an SVG is markup, and pure markup can freeze MC without
 * any script. A `<use>` tree (8 levels of 10 copies) kept MC's main thread
 * busy for more than 20 s, measured as the host page's frame timer
 * (Chromium, Chrome and WebKit — the Safari engine that every iPhone browser
 * uses). Each way of drawing it was tried, with a click-to-show tap in mind:
 *  - `<img>` in MC's page (what FilePreview did before): frozen in every
 *    engine.
 *  - `<img>` in a sandboxed `srcdoc` frame, or the SVG as a sandboxed frame's
 *    own document: frozen in WebKit. Desktop Chrome puts a sandboxed frame in
 *    a process of its own, so MC stays free there — but WebKit has no such
 *    isolation, so the phone would still freeze.
 *  - A frame from another site: frozen in WebKit as well (and MC has no
 *    second site to serve it from).
 *  - Drawing it off the main thread (`createImageBitmap` in a worker): no
 *    engine decodes an SVG there.
 * No way of drawing kept MC free in WebKit, so a tap would only move the
 * freeze, not prevent it. The source view is safe: it is text, nothing of it
 * is parsed as markup. The downloaded file opens outside MC (Files app,
 * another tab), so a hostile one cannot stop MC.
 *
 * Shared by every FilePreview caller (task deliverables, folder deliverables,
 * the workspace tab, the Files page, bench challenges).
 */
import type { ReactNode } from "react";
import { useTranslations } from "next-intl";
import { ShieldAlert } from "lucide-react";
import { C } from "@/lib/colors";

/** File endings FilePreview shows through this view instead of drawing. */
export const SVG_EXTS: ReadonlySet<string> = new Set(["svg"]);

/** The source view is capped too: highlighted XML with line numbers is
 *  several elements per token, and a 10 MB SVG (a big chart, no bomb needed)
 *  blocked MC for more than 21 s that way, a 2 MB one for 3–6 s. Only the
 *  start of a longer file is read and shown, as plain text. */
export const SVG_SOURCE_MAX_BYTES = 100 * 1024;
/** Lines shown at most (a file of very short lines is cut here first). */
export const SVG_SOURCE_MAX_LINES = 2000;
/** Only a file up to this size is highlighted. */
export const SVG_HIGHLIGHT_MAX_BYTES = 16 * 1024;

interface SvgPreviewProps {
  /** The source view (FilePreview's code renderer). */
  code: ReactNode;
  /** Whether FilePreview offers the download next to it — only then does the
   *  note point to it. */
  downloadOffered: boolean;
  /** Only the start of a long file is shown; a second line says so. */
  shortened?: boolean;
}

export function SvgPreview({ code, downloadOffered, shortened = false }: SvgPreviewProps) {
  const t = useTranslations("filePreview.svg");
  return (
    <div data-region="svg-preview" className="flex flex-col gap-2">
      <p className="flex items-start gap-2 text-xs" style={{ color: C.textSecondary }}>
        <ShieldAlert size={14} className="shrink-0 my-px" style={{ color: C.warning }} aria-hidden="true" />
        <span>
          <span>{t("codeOnly")}</span>
          {downloadOffered && (
            <>
              {" "}
              <span>{t("downloadToView")}</span>
            </>
          )}
          {shortened && (
            <span className="block mt-1" style={{ color: C.textMuted }}>
              {t("shortened")}
            </span>
          )}
        </span>
      </p>
      {code}
    </div>
  );
}

"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Loader2, FileX, Download } from "lucide-react";
import SyntaxHighlighter from "react-syntax-highlighter";
import { atomOneDark } from "react-syntax-highlighter/dist/esm/styles/hljs";
import { getToken } from "@/lib/api";
import { useAuthBlob } from "@/hooks/useAuthBlob";
import { C } from "@/lib/colors";
import { VaultMarkdown } from "@/components/vault/VaultMarkdown";
import { HtmlPreview, type FolderUrl } from "./HtmlPreview";
import {
  SvgPreview,
  SVG_EXTS,
  SVG_SOURCE_MAX_BYTES,
  SVG_SOURCE_MAX_LINES,
  SVG_HIGHLIGHT_MAX_BYTES,
} from "./SvgPreview";

// ── Hilfsfunktionen ────────────────────────────────────────────────────────

type PreviewKind =
  | "image"
  | "video"
  | "audio"
  | "pdf"
  | "markdown"
  | "html"
  | "svg"
  | "code"
  | "unsupported";

// Raster pictures only, each with the type its bytes get before they are
// drawn. No "svg": an SVG is markup that can freeze MC when drawn, so it is
// shown as its source (SvgPreview), never as an <img>. The type comes from
// the NAME, not from the server: the deliverable endpoint types a file by a
// symlink's target, so "chart.png" can arrive as image/svg+xml. Typed as
// image/png, the browser refuses SVG bytes (load error, nothing drawn).
const RASTER_TYPES: Readonly<Record<string, string>> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
  bmp: "image/bmp",
  ico: "image/x-icon",
};
const IMAGE_EXTS = new Set(Object.keys(RASTER_TYPES));
// The PDF frame gets a PDF-typed blob for the same reason: a ".pdf" that
// arrives as text/html would otherwise be an HTML page in a blob: frame,
// which runs with MC's own origin.
const PDF_TYPE = "application/pdf";
const VIDEO_EXTS = new Set(["mp4", "mov", "webm", "mkv", "avi"]);
const AUDIO_EXTS = new Set(["mp3", "wav", "ogg", "flac", "m4a", "aac"]);
const MARKDOWN_EXTS = new Set(["md", "markdown"]);
// Rendered in a sandboxed frame (HtmlPreview) with a switch to the source.
const HTML_EXTS = new Set(["html", "htm"]);
const CODE_EXTS = new Set([
  "ts", "tsx", "js", "jsx", "py", "rs", "go", "java", "c", "cpp",
  "h", "cs", "rb", "php", "swift", "kt", "sh", "bash", "zsh",
  "sql", "txt", "json", "yaml", "yml", "toml", "env",
  "css", "scss", "xml", "dockerfile",
]);

function getExtension(path: string): string {
  const name = path.split("/").pop() ?? path;
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
}

function fileName(path: string): string {
  return path.split("/").pop() ?? path;
}

function classifyPath(path: string): PreviewKind {
  const ext = getExtension(path);
  if (SVG_EXTS.has(ext)) return "svg";
  if (IMAGE_EXTS.has(ext)) return "image";
  if (VIDEO_EXTS.has(ext)) return "video";
  if (AUDIO_EXTS.has(ext)) return "audio";
  if (ext === "pdf") return "pdf";
  if (MARKDOWN_EXTS.has(ext)) return "markdown";
  if (HTML_EXTS.has(ext)) return "html";
  if (CODE_EXTS.has(ext)) return "code";
  return "unsupported";
}

function langForExt(ext: string): string {
  const map: Record<string, string> = {
    ts: "typescript", tsx: "typescript", js: "javascript", jsx: "javascript",
    py: "python", rs: "rust", go: "go", java: "java", c: "c", cpp: "cpp",
    h: "c", cs: "csharp", rb: "ruby", php: "php", swift: "swift",
    kt: "kotlin", sh: "bash", bash: "bash", zsh: "bash", sql: "sql",
    md: "markdown", json: "json", yaml: "yaml", yml: "yaml",
    toml: "toml", css: "css", scss: "scss", html: "html", htm: "html", xml: "xml", svg: "xml",
  };
  return map[ext] ?? "plaintext";
}

// ── Authenticated Fetch Hooks ──────────────────────────────────────────────
// (useAuthBlob lebt jetzt in hooks/useAuthBlob.ts — auch die Chat-Anhaenge
//  brauchen ihn.)

/** Reads at most `maxBytes` of a response as UTF-8 text, then stops the
 *  download. A cut inside a multi-byte character drops that character
 *  (the decoder is never flushed after a cut), so no U+FFFD appears. */
async function readTextHead(r: Response, maxBytes: number): Promise<{ text: string; truncated: boolean }> {
  const reader = r.body?.getReader();
  if (!reader) {
    const all = await r.text();
    const bytes = new TextEncoder().encode(all);
    if (bytes.length <= maxBytes) return { text: all, truncated: false };
    return { text: new TextDecoder().decode(bytes.subarray(0, maxBytes), { stream: true }), truncated: true };
  }
  const decoder = new TextDecoder();
  let text = "";
  let read = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return { text: text + decoder.decode(), truncated: false };
    if (read + value.byteLength > maxBytes) {
      text += decoder.decode(value.subarray(0, maxBytes - read), { stream: true });
      reader.cancel().catch(() => undefined);
      return { text, truncated: true };
    }
    read += value.byteLength;
    text += decoder.decode(value, { stream: true });
  }
}

/** Fetches a text file with the Bearer header. With `maxBytes`, only the
 *  start of a longer file is read (`truncated` says so). */
function useAuthText(
  url: string | null,
  maxBytes?: number,
): { text: string | null; truncated: boolean; loading: boolean; error: boolean } {
  const [text, setText] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (!url) return;
    let active = true;
    setLoading(true);
    setError(false);
    fetch(url, { headers: { Authorization: `Bearer ${getToken()}` } })
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status}`);
        return maxBytes === undefined ? r.text().then((t) => ({ text: t, truncated: false })) : readTextHead(r, maxBytes);
      })
      .then((res) => {
        if (!active) return;
        setText(res.text);
        setTruncated(res.truncated);
        setLoading(false);
      })
      .catch(() => { if (active) { setError(true); setLoading(false); } });
    return () => { active = false; };
  }, [url, maxBytes]);

  return { text, truncated, loading, error };
}

/** The first `maxLines` lines of `text`, and whether anything was left out. */
function headLines(text: string, maxLines: number): { text: string; cut: boolean } {
  let at = -1;
  for (let i = 0; i < maxLines; i++) {
    at = text.indexOf("\n", at + 1);
    if (at < 0) return { text, cut: false };
  }
  // Exactly maxLines lines, ending in a newline: nothing left out.
  if (at === text.length - 1) return { text, cut: false };
  return { text: text.slice(0, at), cut: true };
}

/**
 * Download control — fetches the file as a blob WITH the Bearer header, then
 * triggers a browser download via a temporary object-URL anchor. Works on every
 * device (no native dependency). Always offered, so even unsupported types and
 * mobile users can get the file out.
 */
function DownloadButton({ fileUrl, path }: { fileUrl: string; path: string }) {
  const t = useTranslations("filePreview");
  const [busy, setBusy] = useState(false);

  async function handleDownload() {
    if (busy) return;
    setBusy(true);
    try {
      const res = await fetch(fileUrl, { headers: { Authorization: `Bearer ${getToken()}` } });
      if (!res.ok) throw new Error(`${res.status}`);
      const blob = await res.blob();
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = objectUrl;
      a.download = fileName(path);
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(objectUrl);
    } catch {
      // silent — the button is best-effort; nothing destructive on failure
    } finally {
      setBusy(false);
    }
  }

  return (
    <button
      onClick={handleDownload}
      disabled={busy}
      className="inline-flex items-center gap-1.5 min-h-11 md:min-h-8 px-2.5 py-1.5 rounded-md text-xs font-medium transition-colors cursor-pointer disabled:opacity-60"
      style={{ background: C.bgElevated, color: C.textSecondary, border: `1px solid ${C.border}` }}
      onMouseEnter={(e) => (e.currentTarget.style.color = C.textPrimary)}
      onMouseLeave={(e) => (e.currentTarget.style.color = C.textSecondary)}
      title={t("downloadTitle", { name: fileName(path) })}
    >
      {busy ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />}
      {t("download")}
    </button>
  );
}

// ── Sub-Renderer ───────────────────────────────────────────────────────────

function CodePreview({ fileUrl, path }: { fileUrl: string; path: string }) {
  const { text, loading, error } = useAuthText(fileUrl);

  if (loading) return <PreviewLoader />;
  if (error || text === null) return <PreviewError />;

  return <CodeView text={text} path={path} />;
}

/** `highlight={false}` shows the text plain ("text" skips the highlighter):
 *  about two elements per line instead of several per token. */
function CodeView({ text, path, highlight = true }: { text: string; path: string; highlight?: boolean }) {
  const lang = highlight ? langForExt(getExtension(path)) : "text";
  return (
    <div style={{ maxHeight: 400, overflow: "auto", borderRadius: 6 }}>
      <SyntaxHighlighter
        language={lang}
        style={atomOneDark}
        // Code stays on a dark ground in both modes: atomOneDark's syntax
        // colours are built for it (ADR-087).
        // No ligatures: a code font would draw "<!--" or "=>" as one arrow
        // glyph, and code must show exactly what is typed (round-1 t4 N1).
        customStyle={{ margin: 0, fontSize: 12, background: C.codeBg, padding: "12px 16px", fontVariantLigatures: "none" }}
        codeTagProps={{ style: { fontVariantLigatures: "none" } }}
        showLineNumbers
        lineNumberStyle={{ color: C.codeDim, minWidth: "2.5em" }}
      >
        {text}
      </SyntaxHighlighter>
    </div>
  );
}

/** HTML: fetched once (with the Bearer header), then shown as the rendered
 *  page in a sandboxed frame — or as the source, via the switch. */
function HtmlFilePreview({ fileUrl, path, folderUrl }: { fileUrl: string; path: string; folderUrl?: FolderUrl }) {
  const { text, loading, error } = useAuthText(fileUrl);

  if (loading) return <PreviewLoader />;
  if (error || text === null) return <PreviewError />;

  // One preview per file: another file of a folder gets its own folder URL.
  return (
    <HtmlPreview
      key={fileUrl}
      html={text}
      name={fileName(path)}
      code={<CodeView text={text} path={path} />}
      folderUrl={folderUrl}
    />
  );
}

/** SVG: fetched as TEXT (with the Bearer header) and shown as its source,
 *  never drawn — no object URL, no <img>, no frame (see SvgPreview).
 *  The source view has a cost too (several elements per token), so only the
 *  start of a long file is read and shown, plain; only a small file is
 *  highlighted. */
function SvgFilePreview({ fileUrl, path, downloadOffered }: { fileUrl: string; path: string; downloadOffered: boolean }) {
  const { text, truncated, loading, error } = useAuthText(fileUrl, SVG_SOURCE_MAX_BYTES);

  if (loading) return <PreviewLoader />;
  if (error || text === null) return <PreviewError />;

  const head = headLines(text, SVG_SOURCE_MAX_LINES);
  const shortened = truncated || head.cut;
  const highlight = !shortened && new TextEncoder().encode(text).length <= SVG_HIGHLIGHT_MAX_BYTES;
  return (
    <SvgPreview
      code={<CodeView text={head.text} path={path} highlight={highlight} />}
      downloadOffered={downloadOffered}
      shortened={shortened}
    />
  );
}

/** A raster picture; bytes that are no picture (a load error) show the
 *  error state instead of a broken-image icon. */
function RasterImage({ src, alt }: { src: string; alt: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) return <PreviewError />;
  return (
    <img
      src={src}
      alt={alt}
      onError={() => setFailed(true)}
      style={{ maxWidth: "100%", maxHeight: 400, borderRadius: 6, objectFit: "contain" }}
    />
  );
}

function MarkdownPreview({ fileUrl }: { fileUrl: string }) {
  const { text, loading, error } = useAuthText(fileUrl);

  if (loading) return <PreviewLoader />;
  if (error || text === null) return <PreviewError />;

  return (
    <div className="overflow-y-auto" style={{ maxHeight: 500 }}>
      <VaultMarkdown content={text} />
    </div>
  );
}

function PreviewLoader() {
  const t = useTranslations("filePreview");
  return (
    <div role="status" className="flex items-center justify-center py-8">
      <Loader2 size={16} className="animate-spin" style={{ color: C.textMuted }} aria-hidden="true" />
      <span className="sr-only">{t("loading")}</span>
    </div>
  );
}

function PreviewError() {
  const t = useTranslations("filePreview");
  return (
    <div className="flex flex-col items-center justify-center py-6 gap-1.5">
      <FileX size={16} style={{ color: C.textMuted }} />
      <p className="text-xs" style={{ color: C.textMuted }}>{t("loadFailed")}</p>
    </div>
  );
}

// ── Main Component ─────────────────────────────────────────────────────────

interface FilePreviewProps {
  /** URL zum Backend-Content-Endpoint (Deliverable-File ODER /api/v1/files/content). */
  fileUrl: string;
  /** Originalpfad der Datei (für Extension-Erkennung und Code-Sprache). */
  path: string;
  /** Download-Anker immer anzeigen (Default true). Mobile-tauglicher Fallback
   *  für jeden Dateityp — funktioniert ohne native Abhängigkeit. */
  showDownload?: boolean;
  /** Deliverables only: mints the signed folder URL an HTML page needs when
   *  it links its own CSS/images/scripts (HtmlPreview). Other callers leave it
   *  out and HTML stays a single-file srcdoc preview. */
  htmlFolderUrl?: FolderUrl;
}

export function FilePreview({ fileUrl, path, showDownload = true, htmlFolderUrl }: FilePreviewProps) {
  const t = useTranslations("filePreview");
  const kind = classifyPath(path);
  const { blobUrl, loading, error } = useAuthBlob(
    kind === "image" || kind === "video" || kind === "audio" || kind === "pdf" ? fileUrl : null,
    // The type of the NAME, never the server's (see RASTER_TYPES).
    kind === "image" ? RASTER_TYPES[getExtension(path)] : kind === "pdf" ? PDF_TYPE : undefined,
  );

  const download = showDownload ? (
    <div className="flex justify-end mb-2">
      <DownloadButton fileUrl={fileUrl} path={path} />
    </div>
  ) : null;

  // Text-based previews handle their own loading; binary kinds share useAuthBlob.
  let body: React.ReactNode;

  if (kind === "markdown") {
    body = <MarkdownPreview fileUrl={fileUrl} />;
  } else if (kind === "html") {
    body = <HtmlFilePreview fileUrl={fileUrl} path={path} folderUrl={htmlFolderUrl} />;
  } else if (kind === "svg") {
    body = <SvgFilePreview fileUrl={fileUrl} path={path} downloadOffered={showDownload} />;
  } else if (kind === "code") {
    body = <CodePreview fileUrl={fileUrl} path={path} />;
  } else if (kind === "unsupported") {
    body = (
      <div className="flex flex-col items-center justify-center py-6 gap-1">
        <FileX size={16} style={{ color: C.textMuted }} />
        <p className="text-xs" style={{ color: C.textMuted }}>
          {t("noPreview")}
        </p>
      </div>
    );
  } else if (loading) {
    body = <PreviewLoader />;
  } else if (error || !blobUrl) {
    body = <PreviewError />;
  } else if (kind === "image") {
    body = <RasterImage key={blobUrl} src={blobUrl} alt={fileName(path)} />;
  } else if (kind === "video") {
    body = (
      <video
        src={blobUrl}
        controls
        style={{ width: "100%", maxHeight: 360, borderRadius: 6, background: C.bgDeep }}
      />
    );
  } else if (kind === "audio") {
    body = <audio src={blobUrl} controls style={{ width: "100%", marginTop: 4 }} />;
  } else if (kind === "pdf") {
    body = (
      <iframe
        src={blobUrl}
        title={t("pdfTitle")}
        style={{ width: "100%", height: 500, border: "none", borderRadius: 6, background: "transparent" }}
      />
    );
  } else {
    body = null;
  }

  return (
    <div>
      {download}
      {body}
    </div>
  );
}

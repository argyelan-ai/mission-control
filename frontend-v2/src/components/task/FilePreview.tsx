"use client";

import { useEffect, useState } from "react";
import { Loader2, FileX, Download } from "lucide-react";
import SyntaxHighlighter from "react-syntax-highlighter";
import { atomOneDark } from "react-syntax-highlighter/dist/esm/styles/hljs";
import { getToken } from "@/lib/api";
import { C } from "@/lib/colors";
import { VaultMarkdown } from "@/components/vault/VaultMarkdown";

// ── Hilfsfunktionen ────────────────────────────────────────────────────────

type PreviewKind =
  | "image"
  | "video"
  | "audio"
  | "pdf"
  | "markdown"
  | "code"
  | "unsupported";

const IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico"]);
const VIDEO_EXTS = new Set(["mp4", "mov", "webm", "mkv", "avi"]);
const AUDIO_EXTS = new Set(["mp3", "wav", "ogg", "flac", "m4a", "aac"]);
const MARKDOWN_EXTS = new Set(["md", "markdown"]);
const CODE_EXTS = new Set([
  "ts", "tsx", "js", "jsx", "py", "rs", "go", "java", "c", "cpp",
  "h", "cs", "rb", "php", "swift", "kt", "sh", "bash", "zsh",
  "sql", "txt", "json", "yaml", "yml", "toml", "env",
  "css", "scss", "html", "xml", "dockerfile",
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
  if (IMAGE_EXTS.has(ext)) return "image";
  if (VIDEO_EXTS.has(ext)) return "video";
  if (AUDIO_EXTS.has(ext)) return "audio";
  if (ext === "pdf") return "pdf";
  if (MARKDOWN_EXTS.has(ext)) return "markdown";
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
    toml: "toml", css: "css", scss: "scss", html: "html", xml: "xml",
  };
  return map[ext] ?? "plaintext";
}

// ── Authenticated Fetch Hooks ──────────────────────────────────────────────

function useAuthBlob(url: string | null): { blobUrl: string | null; loading: boolean; error: boolean } {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (!url) return;
    let active = true;
    let objectUrl: string | null = null;
    setLoading(true);
    setError(false);

    fetch(url, { headers: { Authorization: `Bearer ${getToken()}` } })
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.blob();
      })
      .then((blob) => {
        if (!active) return;
        objectUrl = URL.createObjectURL(blob);
        setBlobUrl(objectUrl);
        setLoading(false);
      })
      .catch(() => {
        if (!active) return;
        setError(true);
        setLoading(false);
      });

    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      setBlobUrl(null);
    };
  }, [url]);

  return { blobUrl, loading, error };
}

function useAuthText(url: string | null): { text: string | null; loading: boolean; error: boolean } {
  const [text, setText] = useState<string | null>(null);
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
        return r.text();
      })
      .then((t) => { if (active) { setText(t); setLoading(false); } })
      .catch(() => { if (active) { setError(true); setLoading(false); } });
    return () => { active = false; };
  }, [url]);

  return { text, loading, error };
}

/**
 * Download control — fetches the file as a blob WITH the Bearer header, then
 * triggers a browser download via a temporary object-URL anchor. Works on every
 * device (no native dependency). Always offered, so even unsupported types and
 * mobile users can get the file out.
 */
function DownloadButton({ fileUrl, path }: { fileUrl: string; path: string }) {
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
      className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-xs font-medium transition-colors cursor-pointer disabled:opacity-60"
      style={{ background: C.bgElevated, color: C.textSecondary, border: `1px solid ${C.border}` }}
      onMouseEnter={(e) => (e.currentTarget.style.color = C.textPrimary)}
      onMouseLeave={(e) => (e.currentTarget.style.color = C.textSecondary)}
      title={`${fileName(path)} herunterladen`}
    >
      {busy ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />}
      Herunterladen
    </button>
  );
}

// ── Sub-Renderer ───────────────────────────────────────────────────────────

function CodePreview({ fileUrl, path }: { fileUrl: string; path: string }) {
  const { text, loading, error } = useAuthText(fileUrl);

  if (loading) return <PreviewLoader />;
  if (error || text === null) return <PreviewError />;

  const lang = langForExt(getExtension(path));
  return (
    <div style={{ maxHeight: 400, overflow: "auto", borderRadius: 6 }}>
      <SyntaxHighlighter
        language={lang}
        style={atomOneDark}
        customStyle={{ margin: 0, fontSize: 12, background: C.bgBase, padding: "12px 16px" }}
        showLineNumbers
        lineNumberStyle={{ color: C.textDim, minWidth: "2.5em" }}
      >
        {text}
      </SyntaxHighlighter>
    </div>
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
  return (
    <div className="flex items-center justify-center py-8">
      <Loader2 size={16} className="animate-spin" style={{ color: C.textMuted }} />
    </div>
  );
}

function PreviewError() {
  return (
    <div className="flex flex-col items-center justify-center py-6 gap-1.5">
      <FileX size={16} style={{ color: C.textMuted }} />
      <p className="text-xs" style={{ color: C.textMuted }}>Datei konnte nicht geladen werden</p>
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
}

export function FilePreview({ fileUrl, path, showDownload = true }: FilePreviewProps) {
  const kind = classifyPath(path);
  const { blobUrl, loading, error } = useAuthBlob(
    kind === "image" || kind === "video" || kind === "audio" || kind === "pdf" ? fileUrl : null,
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
  } else if (kind === "code") {
    body = <CodePreview fileUrl={fileUrl} path={path} />;
  } else if (kind === "unsupported") {
    body = (
      <div className="flex flex-col items-center justify-center py-6 gap-1">
        <FileX size={16} style={{ color: C.textMuted }} />
        <p className="text-xs" style={{ color: C.textMuted }}>
          Keine Vorschau für diesen Dateityp — bitte herunterladen.
        </p>
      </div>
    );
  } else if (loading) {
    body = <PreviewLoader />;
  } else if (error || !blobUrl) {
    body = <PreviewError />;
  } else if (kind === "image") {
    body = (
      <img
        src={blobUrl}
        alt={fileName(path)}
        style={{ maxWidth: "100%", maxHeight: 400, borderRadius: 6, objectFit: "contain" }}
      />
    );
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
        title="PDF Preview"
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

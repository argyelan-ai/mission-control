import {
  File, FileText, FileCode, FileImage, FileVideo, FileAudio,
  FileArchive, Folder, type LucideIcon,
} from "lucide-react";
import { BRAND, C } from "@/lib/colors";

// ── Extension classification ────────────────────────────────────────────────

const EXT_GROUPS: Record<string, "image" | "video" | "audio" | "archive" | "code" | "doc"> = {};
const reg = (group: typeof EXT_GROUPS[string], exts: string[]) =>
  exts.forEach((e) => { EXT_GROUPS[e] = group; });

reg("image", ["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico", "avif"]);
reg("video", ["mp4", "mov", "webm", "mkv", "avi"]);
reg("audio", ["mp3", "wav", "ogg", "flac", "m4a", "aac"]);
reg("archive", ["zip", "tar", "gz", "tgz", "rar", "7z"]);
reg("code", [
  "ts", "tsx", "js", "jsx", "py", "rs", "go", "java", "c", "cpp", "h", "cs",
  "rb", "php", "swift", "kt", "sh", "bash", "zsh", "sql", "css", "scss",
  "html", "xml", "json", "yaml", "yml", "toml", "env", "dockerfile",
]);
reg("doc", ["md", "markdown", "txt", "pdf", "doc", "docx", "csv", "xlsx"]);

export function getExtension(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
}

/** True for extensions that render as a real thumbnail in grid view. */
export function isImageFile(name: string): boolean {
  return EXT_GROUPS[getExtension(name)] === "image";
}

/** Lucide icon for a file/folder entry, chosen by extension group. */
export function fileIcon(name: string, isDirectory: boolean): LucideIcon {
  if (isDirectory) return Folder;
  const group = EXT_GROUPS[getExtension(name)];
  switch (group) {
    case "image": return FileImage;
    case "video": return FileVideo;
    case "audio": return FileAudio;
    case "archive": return FileArchive;
    case "code": return FileCode;
    case "doc": return FileText;
    default: return File;
  }
}

/** Brand color for known languages/extensions (from colors.ts BRAND), else a
 *  neutral muted tone. Directories use the teal accent. */
export function fileIconColor(name: string, isDirectory: boolean): string {
  if (isDirectory) return C.accent;
  const ext = getExtension(name);
  // BRAND covers language identities (typescript, python, css, …) + a few more.
  const brandKey: Record<string, keyof typeof BRAND> = {
    ts: "typescript", tsx: "typescript", js: "javascript", jsx: "javascript",
    py: "python", rs: "rust", go: "golang", java: "java", css: "css",
    scss: "scss", html: "html", json: "json", yaml: "yaml", yml: "yaml",
    md: "markdown", markdown: "markdown", sh: "shell", bash: "shell",
    zsh: "shell", sql: "sql", env: "env",
  };
  const key = brandKey[ext];
  return key ? BRAND[key] : C.textMuted;
}

// ── Formatting ──────────────────────────────────────────────────────────────

/** Humanize a byte count (1024-based, de-CH-ish: "1.2 KB"). */
export function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

/** Convert a unix-epoch (seconds) mtime to an ISO string for timeAgo(). */
export function mtimeToIso(mtime: number): string {
  return new Date(mtime * 1000).toISOString();
}

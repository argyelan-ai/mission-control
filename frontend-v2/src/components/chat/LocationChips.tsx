"use client";

/**
 * LocationChips — the files a tool call touched, as copyable chips.
 *
 * Tap copies the full path. Deliberately not "open": the path lives in the
 * agent's own filesystem (container or host), and MC's file viewer reads
 * fixed roots — guessing the mapping would open the wrong file or none.
 * The chip shows the file name; the whole path is its title and what lands
 * on the clipboard.
 */
import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Check, Copy } from "lucide-react";
import { C } from "@/lib/colors";

function basename(path: string): string {
  const trimmed = path.replace(/\/+$/, "");
  const i = trimmed.lastIndexOf("/");
  return i < 0 ? trimmed : trimmed.slice(i + 1) || trimmed;
}

/** Clipboard API where it exists (HTTPS, the phone URL); a hidden textarea
 *  where it does not (plain-HTTP LAN access). */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the legacy path */
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand?.("copy") ?? false;
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}

export function LocationChips({ paths }: { paths: string[] }) {
  const t = useTranslations("sessions.toolCard");
  const [copied, setCopied] = useState<{ path: string; ok: boolean } | null>(null);
  const timer = useRef<number | null>(null);
  useEffect(() => () => {
    if (timer.current) window.clearTimeout(timer.current);
  }, []);

  if (paths.length === 0) return null;

  async function onCopy(path: string) {
    const ok = await copyText(path);
    setCopied({ path, ok });
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopied(null), 1600);
  }

  return (
    <div className="flex flex-wrap gap-2" data-testid="tool-locations" role="group" aria-label={t("files")}>
      {paths.map((path) => {
        const state = copied?.path === path ? copied : null;
        return (
          <button
            key={path}
            type="button"
            onClick={() => void onCopy(path)}
            title={path}
            aria-label={t("copyPath", { path })}
            className="inline-flex items-center gap-1 max-w-full min-h-[44px] md:min-h-0 px-2 py-1 rounded-sm text-xs font-mono cursor-pointer bg-transparent"
            style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
          >
            {state?.ok ? (
              <Check size={12} className="shrink-0" aria-hidden="true" />
            ) : (
              <Copy size={12} className="shrink-0" style={{ color: C.textMuted }} aria-hidden="true" />
            )}
            {/* Name and confirmation share ONE grid cell: the chip is always as
                wide as the wider of the two, so swapping the label to "Copied"
                never shifts the row, and no second element appears that could
                read as another file. */}
            <span className="grid min-w-0">
              <span
                className="truncate [grid-area:1/1]"
                style={{ visibility: state ? "hidden" : "visible" }}
                aria-hidden={state ? true : undefined}
              >
                {basename(path)}
              </span>
              <span
                className="truncate [grid-area:1/1]"
                style={{ visibility: state ? "visible" : "hidden" }}
                aria-hidden={state ? undefined : true}
                aria-live="polite"
              >
                {state ? (state.ok ? t("copied") : t("copyFailed")) : ""}
              </span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

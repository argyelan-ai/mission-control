"use client";

import { Link as LinkIcon, ArrowUpRight } from "lucide-react";
import type { UrlCardData } from "./types";
import { CardShell } from "./CardShell";
import { C } from "@/lib/colors";

/**
 * UrlCard — external link surfaced by Jarvis. Click opens in a new tab.
 * Favicon comes from Google's s2 service (free, no auth) — falls back
 * gracefully to the link icon if the domain blocks it.
 */
export function UrlCard({
  data,
  title,
  onClose,
}: {
  data: UrlCardData;
  title?: string | null;
  onClose: () => void;
}) {
  const displayTitle = title || data.domain || data.url;
  const domain = data.domain || (() => {
    try {
      return new URL(data.url).hostname;
    } catch {
      return data.url;
    }
  })();
  const favicon = `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=32`;

  return (
    <CardShell
      onClose={onClose}
      icon={
        // Favicon with link-icon fallback. img-tag is fine here — Next/image
        // would over-engineer a 16px static asset.
        <img
          src={favicon}
          alt=""
          width={13}
          height={13}
          className="rounded-sm"
          onError={(e) => {
            const t = e.currentTarget;
            t.style.display = "none";
            const sib = t.nextElementSibling as HTMLElement | null;
            if (sib) sib.style.display = "inline-block";
          }}
        />
      }
      kind="url"
    >
      <a
        href={data.url}
        target="_blank"
        rel="noopener noreferrer"
        className="flex items-start gap-1.5 group min-w-0 flex-1"
      >
        <div className="min-w-0 flex-1">
          <div
            className="text-[11px] font-medium leading-snug truncate group-hover:text-white transition-colors"
            style={{ color: "var(--color-text-primary)" }}
          >
            {displayTitle}
          </div>
          <div
            className="text-[10px] mt-0.5 truncate"
            style={{ color: "var(--color-text-muted)" }}
          >
            {domain}
          </div>
        </div>
        <ArrowUpRight
          size={11}
          className="mt-0.5 opacity-50 group-hover:opacity-100 shrink-0"
          style={{ color: "var(--color-text-secondary)" }}
        />
      </a>
      {/* Hidden fallback icon — gets unhidden by favicon onError */}
      <LinkIcon
        size={13}
        style={{ color: C.accent, display: "none" }}
      />
    </CardShell>
  );
}

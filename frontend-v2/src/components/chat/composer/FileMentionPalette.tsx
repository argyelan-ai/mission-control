"use client";

import type { CSSProperties, Ref } from "react";
import { useTranslations } from "next-intl";
import { FileText } from "lucide-react";
import { C } from "@/lib/colors";
import { PopoverPortal } from "@/lib/anchoredPopover";
import type { ChatFile, ChatFileStatus } from "./chatFiles";

interface FileMentionPaletteProps {
  paletteRef: Ref<HTMLDivElement>;
  /** Fixed coordinates from `useAnchoredPosition` (anchored on the pill). */
  style: CSSProperties;
  files: ChatFile[];
  status: ChatFileStatus;
  highlightedIndex: number;
  onHover: (index: number) => void;
  onSelect: (file: ChatFile) => void;
}

/**
 * The list the composer's `@` opens: files of the chat's working folder,
 * best match first. Same place, surface and keyboard model as the slash
 * palette (Composer drives the keys); rows are 44 px tall on the phone so a
 * thumb hits them, compact from `md` on.
 *
 * Choosing never moves focus out of the textarea (pointer-down is
 * swallowed), so the caret and the phone keyboard stay where they were.
 */
export function FileMentionPalette({
  paletteRef,
  style,
  files,
  status,
  highlightedIndex,
  onHover,
  onSelect,
}: FileMentionPaletteProps) {
  const t = useTranslations("sessions");
  const message =
    files.length > 0
      ? null
      : status === "no_folder"
        ? t("fileMentionNoFolder")
        : status === "error"
          ? t("fileMentionError")
          : status === "ready"
            ? t("fileMentionNoMatch")
            : t("fileMentionSearching");

  return (
    <PopoverPortal>
      <div
        ref={paletteRef}
        data-testid="mention-palette"
        role="listbox"
        aria-label={t("fileMentionList")}
        className="rounded-md overflow-hidden corner-ticks z-50"
        style={{
          ...style,
          backgroundColor: C.bgElevated,
          border: `1px solid ${C.border}`,
          boxShadow: "var(--shadow-elevated)",
        }}
      >
        {message ? (
          <div
            data-testid="mention-palette-message"
            role="status"
            className="px-3 py-3 text-xs"
            style={{ color: C.textMuted }}
          >
            {message}
          </div>
        ) : (
          <div className="max-h-56 overflow-y-auto p-1.5">
            {files.map((file, i) => {
              const dir = file.path.slice(0, Math.max(0, file.path.length - file.name.length - 1));
              const active = i === highlightedIndex;
              return (
                <button
                  key={file.path}
                  type="button"
                  role="option"
                  aria-selected={active}
                  data-testid={`mention-item-${file.path}`}
                  data-highlighted={active}
                  onPointerDown={(e) => e.preventDefault()}
                  onMouseEnter={() => onHover(i)}
                  onClick={() => onSelect(file)}
                  className="w-full flex items-center gap-2 px-2 min-h-11 md:min-h-0 md:py-1.5 rounded-sm text-xs cursor-pointer text-left"
                  style={{ backgroundColor: active ? C.accentSubtle : "transparent" }}
                >
                  <FileText size={14} className="shrink-0" style={{ color: C.textMuted }} />
                  <span className="font-mono truncate min-w-0" style={{ color: C.textPrimary }}>
                    {file.name}
                  </span>
                  {dir && (
                    <span className="font-mono truncate min-w-0 flex-1" style={{ color: C.textMuted }}>
                      {dir}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        )}
      </div>
    </PopoverPortal>
  );
}

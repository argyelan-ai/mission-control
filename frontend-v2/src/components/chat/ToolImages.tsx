"use client";

/**
 * ToolImages — the pictures a tool produced, as thumbnails on its card
 * (chat package P2). Tap opens the full-screen viewer.
 *
 * Visible WITHOUT opening the card: "the agent took a screenshot" is the
 * point of the tool call, the JSON behind it is not. One picture shows
 * larger; several share one row, the fourth slot counts the rest.
 * Each thumbnail reserves the picture's own shape (width/height from the
 * reference) so the chat does not jump when it loads.
 */
import { useState } from "react";
import { useTranslations } from "next-intl";
import { ImageOff } from "lucide-react";
import { C, alpha } from "@/lib/colors";
import type { ToolImage } from "@/lib/chatTypes";
import { useAuthBlob } from "@/hooks/useAuthBlob";
import { useChatMediaUrl, type MediaUrl } from "./chatMedia";
import { ImageViewer } from "./ImageViewer";

/** Thumbnails shown before "+N". */
const MAX_TILES = 4;

/** Very tall (phone screenshot) or very wide (banner) pictures are cropped
 *  in the thumbnail, never in the viewer. */
function tileAspect(img: ToolImage): number {
  const ratio = img.width > 0 && img.height > 0 ? img.width / img.height : 1.6;
  return Math.min(2, Math.max(0.6, ratio));
}

function Thumb({
  image,
  mediaUrl,
  label,
  single,
  more,
  onOpen,
}: {
  image: ToolImage;
  mediaUrl: MediaUrl;
  label: string;
  single: boolean;
  more: number;
  onOpen: () => void;
}) {
  const t = useTranslations("sessions.toolCard");
  const { blobUrl, error } = useAuthBlob(mediaUrl(image.id, "thumb"));
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label={more > 0 ? t("moreImages", { count: more }) : label}
      data-testid="tool-image"
      className={`relative block overflow-hidden rounded-md cursor-pointer p-0 min-w-0 ${single ? "h-40 max-w-full" : "w-full h-24"}`}
      style={{
        // Several pictures share one even row (same height, cropped);
        // one picture keeps its own shape.
        aspectRatio: single ? String(tileAspect(image)) : undefined,
        minWidth: 44,
        minHeight: 44,
        background: C.bgElevated,
        border: `1px solid ${C.border}`,
      }}
    >
      {blobUrl && (
        // eslint-disable-next-line @next/next/no-img-element -- blob: URL, not a candidate for next/image
        <img src={blobUrl} alt="" draggable={false} className="block w-full h-full object-cover object-top" />
      )}
      {error && (
        <span className="absolute inset-0 flex items-center justify-center" style={{ color: C.textMuted }}>
          <ImageOff size={16} aria-hidden="true" />
          <span className="sr-only">{t("imageUnavailable")}</span>
        </span>
      )}
      {more > 0 && (
        <span
          className="absolute inset-0 flex items-center justify-center text-sm font-medium tabular-nums"
          style={{ background: alpha(C.term, 0.72), color: C.termFg }}
          aria-hidden="true"
        >
          +{more}
        </span>
      )}
    </button>
  );
}

/** Each picture once, first occurrence wins. Two tool calls can return the
 *  very same picture (live 13.10.2026: example.com and example.org render
 *  identically, so both screenshots had one sha256) — a group strip then
 *  showed it twice under one React key. */
export function uniqueImages(images: ToolImage[] | null | undefined): ToolImage[] {
  const seen = new Set<string>();
  return (images ?? []).filter((img) => !seen.has(img.id) && !!seen.add(img.id));
}

export function ToolImages({ images: given }: { images: ToolImage[] | null | undefined }) {
  const t = useTranslations("sessions.toolCard");
  const mediaUrl = useChatMediaUrl();
  const [open, setOpen] = useState<number | null>(null);
  const images = uniqueImages(given);
  if (!mediaUrl || images.length === 0) return null;

  const overflow = images.length > MAX_TILES;
  const tiles = overflow ? images.slice(0, MAX_TILES) : images;
  const single = images.length === 1;

  return (
    <>
      {/* One picture: its own shape, 160 px tall. Several: equal columns in
          one row (never a sideways scroll on the phone). */}
      <div
        className={single ? "flex min-w-0" : "grid gap-2 min-w-0 max-w-md"}
        style={single ? undefined : { gridTemplateColumns: `repeat(${tiles.length}, minmax(0, 1fr))` }}
        role="group"
        aria-label={t("images")}
        data-testid="tool-images"
      >
        {tiles.map((image, i) => {
          const more = overflow && i === MAX_TILES - 1 ? images.length - (MAX_TILES - 1) : 0;
          return (
            <Thumb
              key={image.id}
              image={image}
              mediaUrl={mediaUrl}
              label={t("openImage", { n: i + 1, total: images.length })}
              single={single}
              more={more}
              onOpen={() => setOpen(i)}
            />
          );
        })}
      </div>
      {open !== null && (
        <ImageViewer
          images={images}
          index={open}
          mediaUrl={mediaUrl}
          onIndex={setOpen}
          onClose={() => setOpen(null)}
        />
      )}
    </>
  );
}

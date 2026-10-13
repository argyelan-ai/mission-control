"use client";

/**
 * Full-screen viewer for a tool's pictures (chat package P2).
 *
 * Phone first: pinch to zoom, one finger pans while zoomed, double tap
 * zooms in/out, drag down to close, swipe sideways to page. Desktop: Escape,
 * arrow keys, mouse wheel. Download and copy sit in the top bar.
 *
 * The stage is dark in both modes (`C.term`): a picture is content, not
 * chrome — the same reason terminals stay dark (DESIGN.md, ADR-087).
 * Pictures are fetched with the chat's Bearer auth (`useAuthBlob`); the
 * thumbnail stands in until the full picture has loaded.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslations } from "next-intl";
import { Check, ChevronLeft, ChevronRight, Copy, Download, ImageOff, X } from "lucide-react";
import { C, alpha } from "@/lib/colors";
import type { ToolImage } from "@/lib/chatTypes";
import { useAuthBlob } from "@/hooks/useAuthBlob";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { downloadName, type MediaUrl } from "./chatMedia";
import {
  DOUBLE_TAP_MS,
  DOUBLE_TAP_SCALE,
  MIN_SCALE,
  clampPan,
  clampScale,
  distance,
  dragFade,
  pinchScale,
  swipeOutcome,
} from "./viewerGestures";

type Point = { x: number; y: number };

/** Movement below this (css px) still counts as a tap. */
const TAP_SLOP = 8;

function canCopyImages(): boolean {
  return (
    typeof window !== "undefined" &&
    "ClipboardItem" in window &&
    typeof navigator !== "undefined" &&
    typeof navigator.clipboard?.write === "function"
  );
}

/** PNG is the one picture type every clipboard takes. `mime` is what the
 *  backend stored (a blob: URL's own type is not reliable everywhere). */
async function asPng(blob: Blob, mime: string): Promise<Blob> {
  if (mime === "image/png") return blob.type === "image/png" ? blob : new Blob([blob], { type: "image/png" });
  const bitmap = await createImageBitmap(blob);
  const canvas = document.createElement("canvas");
  canvas.width = bitmap.width;
  canvas.height = bitmap.height;
  canvas.getContext("2d")?.drawImage(bitmap, 0, 0);
  return await new Promise<Blob>((resolve, reject) =>
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("toBlob"))), "image/png"),
  );
}

function ViewerButton({
  label,
  onClick,
  children,
  testId,
  buttonRef,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
  testId?: string;
  buttonRef?: React.Ref<HTMLButtonElement>;
}) {
  return (
    <button
      ref={buttonRef}
      type="button"
      aria-label={label}
      title={label}
      data-testid={testId}
      onClick={onClick}
      className="inline-flex items-center justify-center min-w-[44px] min-h-[44px] rounded-md bg-transparent border-0 cursor-pointer"
      style={{ color: C.termFg }}
    >
      {children}
    </button>
  );
}

export function ImageViewer({
  images,
  index,
  mediaUrl,
  onIndex,
  onClose,
}: {
  images: ToolImage[];
  index: number;
  mediaUrl: MediaUrl;
  onIndex: (next: number) => void;
  onClose: () => void;
}) {
  const t = useTranslations("sessions.toolCard.viewer");
  const image = images[Math.min(Math.max(index, 0), images.length - 1)];
  const thumb = useAuthBlob(image ? mediaUrl(image.id, "thumb") : null);
  const full = useAuthBlob(image ? mediaUrl(image.id, "full") : null);
  const src = full.blobUrl ?? thumb.blobUrl;
  const failed = full.error && !thumb.blobUrl;

  const [scale, setScale] = useState(MIN_SCALE);
  const [pan, setPan] = useState<Point>({ x: 0, y: 0 });
  const [drag, setDrag] = useState<Point>({ x: 0, y: 0 });
  const [gesturing, setGesturing] = useState(false);
  const [copyState, setCopyState] = useState<"idle" | "ok" | "fail">("idle");

  const stageRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const pointers = useRef(new Map<number, Point>());
  const gesture = useRef({
    start: { x: 0, y: 0 } as Point,
    startPan: { x: 0, y: 0 } as Point,
    startScale: MIN_SCALE,
    startDist: 0,
    startMid: { x: 0, y: 0 } as Point,
    pinched: false,
    moved: false,
    lastTap: 0,
  });

  useBodyScrollLock(true);

  const hasMany = images.length > 1;
  const go = useCallback(
    (step: 1 | -1) => {
      if (!hasMany) return;
      onIndex((index + step + images.length) % images.length);
    },
    [hasMany, index, images.length, onIndex],
  );

  // A new picture starts unzoomed.
  useEffect(() => {
    setScale(MIN_SCALE);
    setPan({ x: 0, y: 0 });
    setDrag({ x: 0, y: 0 });
    setCopyState("idle");
  }, [image?.id]);

  // Focus moves into the dialog and back to the thumbnail afterwards.
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    return () => before?.focus?.();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      } else if (e.key === "ArrowRight") {
        go(1);
      } else if (e.key === "ArrowLeft") {
        go(-1);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [go, onClose]);

  const stageSize = () => {
    const el = stageRef.current;
    return { width: el?.clientWidth ?? 0, height: el?.clientHeight ?? 0 };
  };

  const toggleZoomAt = (p: Point) => {
    if (scale > MIN_SCALE) {
      setScale(MIN_SCALE);
      setPan({ x: 0, y: 0 });
      return;
    }
    const el = stageRef.current;
    const rect = el?.getBoundingClientRect();
    const cx = rect ? rect.left + rect.width / 2 : p.x;
    const cy = rect ? rect.top + rect.height / 2 : p.y;
    const next = DOUBLE_TAP_SCALE;
    setScale(next);
    setPan(clampPan({ x: (cx - p.x) * (next - 1), y: (cy - p.y) * (next - 1) }, next, stageSize()));
  };

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    e.currentTarget.setPointerCapture?.(e.pointerId);
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const g = gesture.current;
    const pts = [...pointers.current.values()];
    setGesturing(true);
    if (pts.length === 2) {
      g.pinched = true;
      g.startDist = distance(pts[0], pts[1]);
      g.startScale = scale;
      g.startPan = pan;
      g.startMid = { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 };
      setDrag({ x: 0, y: 0 });
    } else if (pts.length === 1) {
      g.pinched = false;
      g.moved = false;
      g.start = { x: e.clientX, y: e.clientY };
      g.startPan = pan;
    }
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!pointers.current.has(e.pointerId)) return;
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const g = gesture.current;
    const pts = [...pointers.current.values()];
    if (pts.length >= 2) {
      const next = pinchScale(g.startScale, g.startDist, distance(pts[0], pts[1]));
      const mid = { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 };
      setScale(next);
      setPan(clampPan({ x: g.startPan.x + mid.x - g.startMid.x, y: g.startPan.y + mid.y - g.startMid.y }, next, stageSize()));
      return;
    }
    const d = { x: e.clientX - g.start.x, y: e.clientY - g.start.y };
    if (Math.abs(d.x) > TAP_SLOP || Math.abs(d.y) > TAP_SLOP) g.moved = true;
    if (g.pinched) return;
    if (scale > MIN_SCALE) {
      setPan(clampPan({ x: g.startPan.x + d.x, y: g.startPan.y + d.y }, scale, stageSize()));
    } else {
      setDrag(d);
    }
  };

  const onPointerEnd = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!pointers.current.has(e.pointerId)) return;
    pointers.current.delete(e.pointerId);
    const g = gesture.current;
    const left = [...pointers.current.values()];
    if (left.length === 1) {
      // Pinch became one finger: continue as a pan from here, no jump.
      g.start = left[0];
      g.startPan = pan;
      return;
    }
    if (left.length > 0) return;
    setGesturing(false);
    if (g.pinched) {
      if (scale < MIN_SCALE + 0.05) {
        setScale(MIN_SCALE);
        setPan({ x: 0, y: 0 });
      }
      g.pinched = false;
      return;
    }
    if (!g.moved) {
      const now = Date.now();
      if (now - g.lastTap < DOUBLE_TAP_MS) {
        g.lastTap = 0;
        toggleZoomAt({ x: e.clientX, y: e.clientY });
      } else {
        g.lastTap = now;
      }
      setDrag({ x: 0, y: 0 });
      return;
    }
    const outcome = swipeOutcome(drag.x, drag.y, scale);
    setDrag({ x: 0, y: 0 });
    if (outcome === "close") onClose();
    else if (outcome === "next") go(1);
    else if (outcome === "prev") go(-1);
  };

  const onWheel = (e: React.WheelEvent<HTMLDivElement>) => {
    const next = clampScale(scale * Math.exp(-e.deltaY * 0.002));
    setScale(next);
    setPan((p) => clampPan(p, next, stageSize()));
  };

  const onCopy = () => {
    if (!src || !image) return;
    // The promise goes INTO the ClipboardItem: Safari only accepts a write
    // started synchronously inside the tap.
    const pngPromise = fetch(src)
      .then((r) => r.blob())
      .then((b) => asPng(b, image.mime));
    pngPromise.catch(() => setCopyState("fail"));
    navigator.clipboard
      .write([new ClipboardItem({ "image/png": pngPromise })])
      .then(() => setCopyState("ok"))
      .catch(() => setCopyState("fail"));
  };

  const onDownload = () => {
    if (!src || !image) return;
    const a = document.createElement("a");
    a.href = src;
    a.download = downloadName(image);
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  if (!image || typeof document === "undefined") return null;

  // Dragging down to close carries the picture with the finger and lets the
  // dark stage fade, so the gesture shows what it is about to do.
  const offsetY = scale > MIN_SCALE ? 0 : Math.max(0, drag.y);
  const offsetX = scale > MIN_SCALE ? 0 : drag.x * (hasMany ? 1 : 0.3);
  const transform = `translate3d(${pan.x + offsetX}px, ${pan.y + offsetY}px, 0) scale(${scale})`;
  const counter = hasMany ? t("counter", { n: index + 1, total: images.length }) : null;

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label={t("title")}
      data-testid="image-viewer"
      className="fixed inset-0 z-[200] flex flex-col"
      style={{ background: alpha(C.term, dragFade(offsetY)) }}
    >
      <div className="flex items-center gap-2 px-2 pt-safe">
        <span className="flex-1 min-w-0 pl-2 text-sm tabular-nums" style={{ color: C.termFg }} aria-live="polite">
          {counter}
        </span>
        {canCopyImages() && (
          <ViewerButton
            label={copyState === "ok" ? t("copied") : copyState === "fail" ? t("copyFailed") : t("copy")}
            onClick={onCopy}
            testId="image-viewer-copy"
          >
            {copyState === "ok" ? <Check size={20} aria-hidden="true" /> : <Copy size={20} aria-hidden="true" />}
          </ViewerButton>
        )}
        <ViewerButton label={t("download")} onClick={onDownload} testId="image-viewer-download">
          <Download size={20} aria-hidden="true" />
        </ViewerButton>
        <ViewerButton label={t("close")} onClick={onClose} testId="image-viewer-close" buttonRef={closeRef}>
          <X size={22} aria-hidden="true" />
        </ViewerButton>
      </div>
      <span className="sr-only" aria-live="polite">
        {copyState === "ok" ? t("copied") : copyState === "fail" ? t("copyFailed") : ""}
      </span>

      <div
        ref={stageRef}
        data-testid="image-viewer-stage"
        className="relative flex-1 min-h-0 overflow-hidden touch-none select-none"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerEnd}
        onPointerCancel={onPointerEnd}
        onWheel={onWheel}
      >
        {failed ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2" style={{ color: C.termFg }}>
            <ImageOff size={28} aria-hidden="true" />
            <span className="text-sm">{t("unavailable")}</span>
          </div>
        ) : src ? (
          // eslint-disable-next-line @next/next/no-img-element -- blob: URL, not a candidate for next/image
          <img
            src={src}
            alt={t("alt", { n: index + 1, total: images.length })}
            draggable={false}
            data-testid="image-viewer-img"
            data-full={full.blobUrl ? "true" : undefined}
            className="absolute inset-0 m-auto max-w-full max-h-full object-contain motion-reduce:transition-none"
            style={{
              transform,
              transformOrigin: "center center",
              transition: gesturing ? "none" : "transform 200ms cubic-bezier(0.16,1,0.3,1)",
            }}
          />
        ) : null}
      </div>

      {hasMany && (
        <div className="flex items-center justify-center gap-8 pb-safe">
          <ViewerButton label={t("previous")} onClick={() => go(-1)} testId="image-viewer-prev">
            <ChevronLeft size={24} aria-hidden="true" />
          </ViewerButton>
          <ViewerButton label={t("next")} onClick={() => go(1)} testId="image-viewer-next">
            <ChevronRight size={24} aria-hidden="true" />
          </ViewerButton>
        </div>
      )}
    </div>,
    document.body,
  );
}

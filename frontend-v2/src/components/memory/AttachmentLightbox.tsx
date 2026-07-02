"use client";

/**
 * Phase 5 MSY-03: Full-screen image viewer (Radix Dialog).
 *
 * - Image fills max 90vw × 90vh; blurred dark overlay underneath.
 * - ESC closes (Radix default).
 * - German copy verbatim per UI-SPEC: filename · size below image; "Schliessen" aria-label.
 */
import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";

interface Props {
  open: boolean;
  src: string | null;
  filename: string;
  sizeKb: number;
  onClose: () => void;
}

export function AttachmentLightbox({ open, src, filename, sizeKb, onClose }: Props) {
  return (
    <Dialog.Root open={open} onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay
          className="fixed inset-0 z-50"
          style={{ background: "rgba(0,0,0,0.9)", backdropFilter: "blur(8px)" }}
        />
        <Dialog.Content
          className="fixed inset-0 z-50 flex items-center justify-center p-6"
          aria-label="Anhang Vorschau"
        >
          <Dialog.Title className="sr-only">{filename}</Dialog.Title>
          <Dialog.Description className="sr-only">
            Bildvorschau in voller Grösse — ESC zum Schliessen.
          </Dialog.Description>
          {src && (
            <img
              src={src}
              alt={filename}
              className="max-w-[90vw] max-h-[90vh] object-contain"
            />
          )}
          <div
            className="absolute bottom-6 left-6 text-sm tabular-nums"
            style={{ color: "rgba(255,255,255,0.8)" }}
          >
            {filename} · {sizeKb} KB
          </div>
          <Dialog.Close
            className="absolute top-6 right-6 p-2 rounded-full"
            style={{ background: "rgba(255,255,255,0.1)" }}
            aria-label="Schliessen"
          >
            <X size={20} className="text-white" />
          </Dialog.Close>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

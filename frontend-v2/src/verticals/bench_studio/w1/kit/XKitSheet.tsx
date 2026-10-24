"use client";

// CONTRACT STUB (bench W1, 24.10.2026) — owned by package C (X-Kit P6).
//
// Previews (video 16:9, card 1080×1350), "Alles laden" (kit.zip) + single
// downloads via a download token (real links, Content-Disposition), copy
// buttons for settings text / alt text / facts / link, thread guide. Red
// gate = preview only with the reason + fix button. If the test has a
// truncation warning, the sheet stops the operator first and offers the re-run.
//
// Signature is frozen.
export interface XKitSheetProps {
  testId: string;
  open: boolean;
  onClose: () => void;
}

export function XKitSheet({ open }: XKitSheetProps) {
  if (!open) return null;
  return <div data-testid="bench-xkit-sheet" />;
}

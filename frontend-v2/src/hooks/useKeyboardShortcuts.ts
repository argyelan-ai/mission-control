"use client";

import { useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { useAppStore } from "@/lib/store";

export function useKeyboardShortcuts() {
  const router = useRouter();
  const { toggleSidebar, setCommandPaletteOpen } = useAppStore();
  const chordRef = useRef<string | null>(null);
  const chordTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (
        target.tagName === "INPUT" ||
        target.tagName === "TEXTAREA" ||
        target.isContentEditable
      ) {
        // Allow Cmd+K even in inputs
        if (!(e.metaKey && e.key === "k") && !(e.ctrlKey && e.key === "k")) {
          return;
        }
      }

      const isMeta = e.metaKey || e.ctrlKey;

      if (isMeta) {
        switch (e.key) {
          case "k":
            e.preventDefault();
            setCommandPaletteOpen(true);
            break;
          case "b":
            e.preventDefault();
            toggleSidebar();
            break;
        }
        return;
      }

      // Vim-style G chords (only outside inputs)
      if (chordRef.current === "g") {
        chordRef.current = null;
        if (chordTimeoutRef.current) clearTimeout(chordTimeoutRef.current);

        switch (e.key) {
          case "h": router.push("/"); break;
          case "t": router.push("/tasks"); break;
          case "a": router.push("/agents"); break;
          case "i": router.push("/inbox"); break;
          case "s": router.push("/settings"); break;
        }
        return;
      }

      if (e.key === "g") {
        chordRef.current = "g";
        chordTimeoutRef.current = setTimeout(() => {
          chordRef.current = null;
        }, 1000);
        return;
      }

      switch (e.key) {
        case "?":
          setCommandPaletteOpen(true);
          break;
        case "Escape":
          setCommandPaletteOpen(false);
          break;
      }
    };

    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [router, toggleSidebar, setCommandPaletteOpen]);
}

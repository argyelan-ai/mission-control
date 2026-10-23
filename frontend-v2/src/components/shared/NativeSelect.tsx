"use client";

import type { SelectHTMLAttributes } from "react";
import { ChevronDown } from "lucide-react";
import { C } from "@/lib/colors";

/** A native `<select>` that draws its own box (DESIGN.md "Inputs / Fields").
 *
 *  WebKit (Safari, every iPhone browser) keeps its own control look and
 *  ignores `min-height` on a native select: the bench dialog's selects were
 *  27 px next to 44 px text fields (round-4 t2 SOLL-5). With
 *  `appearance-none` the select takes the field's height; the arrow the
 *  browser no longer draws is a lucide chevron that lets taps through.
 *  The native picker (wheel on the iPhone) stays. */
export function NativeSelect({
  className = "",
  wrapperClassName = "",
  children,
  ...props
}: SelectHTMLAttributes<HTMLSelectElement> & { wrapperClassName?: string }) {
  return (
    <div className={`relative min-w-0 ${wrapperClassName}`}>
      <select {...props} className={`appearance-none pr-8 cursor-pointer ${className}`}>
        {children}
      </select>
      <ChevronDown
        size={16}
        aria-hidden="true"
        className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2"
        style={{ color: C.textMuted }}
      />
    </div>
  );
}

"use client";

/**
 * Phase 5 MSY-02 — MergeCandidateBadge.
 *
 * Small violet pill on a memory card header indicating the entry has a
 * cosine-similarity candidate (merge_candidate_id !== null). Click navigates
 * to / opens the entry's modal where the MergeResolutionPanel auto-expands
 * with the 3 user-confirm actions.
 *
 * Visual contract — see 05-UI-SPEC.md "Component Inventory":
 *   inline-flex pill, 10px text, uppercase semibold + tracking-wider,
 *   teal tint background `${C.accent}1F` + teal border
 *   `C.borderAccent` + accent text `C.accent`,
 *   lucide GitMerge icon at 12px.
 *
 * Motion contract — fade-in only, 150ms (micro-feedback). useReducedMotion
 * guard per the operator's Design-DNA "prefers-reduced-motion respected" rule.
 */

import { GitMerge } from "lucide-react";
import { motion, useReducedMotion } from "framer-motion";
import { C } from "@/lib/colors";

export function MergeCandidateBadge() {
  const prefersReduce = useReducedMotion();
  return (
    <motion.span
      initial={prefersReduce ? false : { opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.15 }}
      className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold uppercase tracking-wider"
      style={{
        background: C.accentSubtle,
        border: `1px solid ${C.borderAccent}`,
        color: C.accent,
      }}
      role="status"
      aria-label="Mögliches Duplikat — anklicken zum Prüfen"
      title="Ähnlicher Eintrag gefunden — anklicken zum Prüfen"
    >
      <GitMerge size={12} aria-hidden />
      <span>MERGE</span>
    </motion.span>
  );
}

"use client";

/**
 * Phase 5 MSY-02 — MergeResolutionPanel.
 *
 * Inline expansion inside MemoryModal body when the displayed entry has
 * `merge_candidate_id !== null`. Renders:
 *   - heading "Mögliches Duplikat" (teal)
 *   - candidate preview card (clickable navigation hint via title + content)
 *   - 3 action buttons:
 *       1. "In bestehenden zusammenführen" (primary, teal gradient)
 *          → POST /knowledge/{id}/merge_into/{candidate_id}
 *          → window.confirm before destructive merge (UI-SPEC accepts for v0.5)
 *       2. "Beide behalten" (neutral ghost)
 *          → POST /knowledge/{id}/keep_both
 *       3. "Als nicht verwandt markieren" (destructive ghost, red text)
 *          → POST /knowledge/{id}/unrelated
 *
 * Motion contract — height + opacity via AnimatePresence; spring-physics
 * 300ms expand. useReducedMotion guard per the operator's Design-DNA.
 *
 * Color: teal accent reserved for the primary CTA only (per UI-SPEC
 * "Accent reserved for" #3). Surface uses teal-tinted glass on a dark
 * background (the operator's Design-DNA "no purple" rule).
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import { api } from "@/lib/api";
import type { BoardMemory } from "@/lib/types";
import { C } from "@/lib/colors";

interface Props {
  entry: BoardMemory;
  onResolved?: () => void;
}

export function MergeResolutionPanel({ entry, onResolved }: Props) {
  const queryClient = useQueryClient();
  const prefersReduce = useReducedMotion();
  const [busy, setBusy] = useState(false);

  const candidateId = entry.merge_candidate_id ?? null;
  const { data: candidate } = useQuery({
    queryKey: ["knowledge-entry", candidateId],
    queryFn: () =>
      candidateId ? api.knowledge.get(candidateId) : Promise.resolve(null),
    enabled: candidateId !== null,
    staleTime: 60_000,
  });

  if (!candidateId) return null;

  async function invalidate() {
    await queryClient.invalidateQueries({ queryKey: ["knowledge"] });
    await queryClient.invalidateQueries({ queryKey: ["knowledge-layer-episodic"] });
    await queryClient.invalidateQueries({ queryKey: ["knowledge-layer-semantic"] });
    await queryClient.invalidateQueries({ queryKey: ["knowledge-layer-agent"] });
    onResolved?.();
  }

  async function onMergeInto() {
    const targetTitle =
      candidate?.entry?.title ??
      candidate?.entry?.content?.slice(0, 40) ??
      "Ziel";
    const sourceLabel = entry.title ?? entry.content.slice(0, 40);
    if (
      !window.confirm(
        `${sourceLabel} in ${targetTitle} zusammenführen? Der Quell-Eintrag wird gelöscht.`,
      )
    ) {
      return;
    }
    setBusy(true);
    try {
      await api.knowledge.mergeInto(entry.id, candidateId!);
      await invalidate();
    } finally {
      setBusy(false);
    }
  }

  async function onKeepBoth() {
    setBusy(true);
    try {
      await api.knowledge.keepBoth(entry.id);
      await invalidate();
    } finally {
      setBusy(false);
    }
  }

  async function onUnrelated() {
    setBusy(true);
    try {
      await api.knowledge.markUnrelated(entry.id);
      await invalidate();
    } finally {
      setBusy(false);
    }
  }

  return (
    <AnimatePresence>
      <motion.section
        initial={prefersReduce ? false : { opacity: 0, height: 0 }}
        animate={{ opacity: 1, height: "auto" }}
        exit={prefersReduce ? { opacity: 0 } : { opacity: 0, height: 0 }}
        transition={{ duration: 0.3, ease: [0.2, 0.8, 0.2, 1] }}
        className="rounded-xl p-4 mt-5"
        style={{
          background: C.accentSubtle,
          border: `1px solid ${C.borderAccent}`,
        }}
      >
        <h3
          className="text-base font-semibold mb-3"
          style={{ color: C.accent }}
        >
          Mögliches Duplikat
        </h3>
        {candidate?.entry && (
          <div
            className="rounded-lg p-3 mb-4"
            style={{
              border: `1px solid ${C.borderAccent}`,
              background: C.accentSubtle,
              borderRadius: 8,
            }}
          >
            <div className="text-sm font-semibold">
              {candidate.entry.title ?? "(ohne Titel)"}
            </div>
            <div className="text-xs opacity-70 line-clamp-2 mt-1">
              {candidate.entry.content}
            </div>
          </div>
        )}
        <div className="flex flex-wrap gap-2">
          <button
            onClick={onMergeInto}
            disabled={busy}
            className="px-4 py-2 rounded-lg text-sm font-semibold disabled:opacity-50 cursor-pointer focus-visible:ring-2 focus-visible:ring-teal-500/50"
            style={{
              background: `linear-gradient(135deg, ${C.accent}, ${C.accentHover})`,
              color: C.bgDeep,
            }}
          >
            In bestehenden zusammenführen
          </button>
          <button
            onClick={onKeepBoth}
            disabled={busy}
            className="px-4 py-2 rounded-lg text-sm border disabled:opacity-50 cursor-pointer focus-visible:ring-2 focus-visible:ring-teal-500/50"
            style={{ borderColor: C.border }}
          >
            Beide behalten
          </button>
          <button
            onClick={onUnrelated}
            disabled={busy}
            className="px-4 py-2 rounded-lg text-sm border disabled:opacity-50 cursor-pointer focus-visible:ring-2 focus-visible:ring-red-500/50"
            style={{
              borderColor: `${C.error}66`,
              color: C.error,
            }}
          >
            Als nicht verwandt markieren
          </button>
        </div>
      </motion.section>
    </AnimatePresence>
  );
}

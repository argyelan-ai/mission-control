"use client";

/**
 * TraversalAnimation — headless effect coordinator for wikilink edge traversal.
 *
 * When `fromPath` and `toPath` are both set:
 *   1. Immediately notifies the parent to set `traversalEdge` on MemoryGraph2D
 *      → the edge glows brand-purple (handled via prop, Option A).
 *   2. After 200 ms: calls graphRef.current.zoomToNodes([toPath]) — camera flies
 *      to the target node (1.2 s spring).
 *   3. After 1500 ms: calls onComplete() — parent clears traversalEdge + updates
 *      selectedPath to toPath.
 *
 * All timeouts are cleaned up on unmount to prevent state-update-after-unmount.
 * The component renders nothing.
 *
 * Usage (in T10 page):
 *   <TraversalAnimation
 *     fromPath={traversal.from}
 *     toPath={traversal.to}
 *     graphRef={graphRef}
 *     onTraversalEdge={(edge) => setTraversalEdge(edge)}
 *     onComplete={() => { setTraversal(null); setTraversalEdge(null); setSelectedPath(traversal.to); }}
 *   />
 */

import { MutableRefObject, useEffect } from "react";
import type { MemoryGraph2DRef } from "./MemoryGraph2D";

export interface TraversalAnimationProps {
  /** Source vault path — null means no active traversal. */
  fromPath: string | null;
  /** Target vault path — null means no active traversal. */
  toPath: string | null;
  /** Ref to the MemoryGraph2D imperative handle (for zoomToNodes). */
  graphRef: MutableRefObject<MemoryGraph2DRef | null>;
  /**
   * Called immediately when traversal starts with the edge descriptor.
   * Parent should set this as `traversalEdge` prop on MemoryGraph2D.
   * Called with null when the animation sequence finishes.
   */
  onTraversalEdge: (edge: { source: string; target: string } | null) => void;
  /** Called after zoom completes — parent navigates to toPath and clears state. */
  onComplete: () => void;
}

export function TraversalAnimation({
  fromPath,
  toPath,
  graphRef,
  onTraversalEdge,
  onComplete,
}: TraversalAnimationProps) {
  useEffect(() => {
    if (!fromPath || !toPath) return;

    // Step 1 — immediately light up the edge (brand-purple glow via prop).
    onTraversalEdge({ source: fromPath, target: toPath });

    // Step 2 — after short delay, fly camera to the target node.
    const t1 = setTimeout(() => {
      graphRef.current?.zoomToNodes([toPath]);
    }, 200);

    // Step 3 — after zoom completes, clear edge glow and hand off control.
    const t2 = setTimeout(() => {
      onTraversalEdge(null);
      onComplete();
    }, 1_500);

    return () => {
      clearTimeout(t1);
      clearTimeout(t2);
      // Clean up edge glow if the component unmounts mid-animation.
      onTraversalEdge(null);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fromPath, toPath]);
  // graphRef is a stable MutableRefObject — no need in deps.
  // onTraversalEdge / onComplete should be stable (useCallback) in the parent.

  return null;
}

"use client";

// CONTRACT STUB (bench W1, 24.10.2026) — owned by package B (live run P4).
//
// One card per contestant: phase, run i/n, elapsed, tokens so far, decode
// tok/s (class A only), wait reason; class B after class A with
// "startet nach Lauf 3"; Stop. Rendered by the result page (package C)
// while test.state is "waiting" or "running"; C polls the detail
// (LIVE_POLL_MS) and passes the fresh record in.
//
// Signature is frozen.
import type { BenchTestDetail } from "../types";

export interface LiveRunProps {
  test: BenchTestDetail;
  /** Called after a successful stop with the updated record. */
  onStopped?: (test: BenchTestDetail) => void;
}

export function LiveRun({ test }: LiveRunProps) {
  return <div data-testid="bench-live-run" data-test-id={test.id} />;
}

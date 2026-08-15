"use client";
import { useEffect, useRef, useState } from "react";

const MAX_SAMPLES = 24;

/** Session-local rolling buffer of GPU utilisation samples for one host.
 *  Honest by construction: starts empty, fills only while the page is open.
 *
 *  `sampledAt` should be the metrics query's `dataUpdatedAt` (a timestamp
 *  that changes on every poll, even when the GPU util value itself repeats —
 *  e.g. an idle box sitting at 0%). Keying the effect on the raw value
 *  instead deduped every unchanged poll away, so a flat line at any value
 *  never grew past its first sample. */
export function useGpuSparkline(
  hostId: string,
  gpuUtilPct: number | null | undefined,
  sampledAt?: number
): number[] {
  const buffers = useRef<Map<string, number[]>>(new Map());
  const [, bump] = useState(0);
  useEffect(() => {
    if (gpuUtilPct == null) return;
    const buf = buffers.current.get(hostId) ?? [];
    buffers.current.set(hostId, [...buf, gpuUtilPct].slice(-MAX_SAMPLES));
    bump((n) => n + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hostId, sampledAt]);
  return buffers.current.get(hostId) ?? [];
}

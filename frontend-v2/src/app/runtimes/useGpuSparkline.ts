"use client";
import { useEffect, useRef, useState } from "react";

const MAX_SAMPLES = 24;

/** Session-local rolling buffer of GPU utilisation samples for one host.
 *  Honest by construction: starts empty, fills only while the page is open. */
export function useGpuSparkline(hostId: string, gpuUtilPct: number | null | undefined): number[] {
  const buffers = useRef<Map<string, number[]>>(new Map());
  const [, bump] = useState(0);
  useEffect(() => {
    if (gpuUtilPct == null) return;
    const buf = buffers.current.get(hostId) ?? [];
    buffers.current.set(hostId, [...buf, gpuUtilPct].slice(-MAX_SAMPLES));
    bump((n) => n + 1);
  }, [hostId, gpuUtilPct]);
  return buffers.current.get(hostId) ?? [];
}

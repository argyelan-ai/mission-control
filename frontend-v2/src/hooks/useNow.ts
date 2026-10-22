"use client";

import { useEffect, useState } from "react";

/**
 * The current time (ms), re-read every `intervalMs` — for labels like
 * "1 min ago" / "for 3 h" that must move on their own. Only the component
 * that calls it re-renders. One place (it lived twice, as local copies in
 * CheckStatus and EntryTaskState).
 */
export function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

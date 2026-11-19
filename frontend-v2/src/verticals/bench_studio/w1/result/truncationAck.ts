// "Trotzdem weiter" on the private truncation warning (operator decision
// 24.10.): once the operator chose to go on with a cut run, neither the
// result page nor the X-Kit sheet stops him again for that test — for this
// browser tab session. The two surfaces cannot pass props to each other
// (XKitSheetProps is frozen), so they share this tiny store.
//
// sessionStorage is only a convenience: it may be missing or throw (private
// mode); the store then lives in memory and the page still works.
import { useCallback, useSyncExternalStore } from "react";

const STORAGE_SLOT = "mc.bench.w1.truncationAck";
const listeners = new Set<() => void>();
let acked: Set<string> | null = null;

function load(): Set<string> {
  if (acked) return acked;
  acked = new Set();
  try {
    const raw = window.sessionStorage.getItem(STORAGE_SLOT);
    if (raw) for (const id of JSON.parse(raw) as string[]) acked.add(id);
  } catch {
    // no storage — memory only
  }
  return acked;
}

function save(set: Set<string>) {
  try {
    window.sessionStorage.setItem(STORAGE_SLOT, JSON.stringify([...set]));
  } catch {
    // no storage — memory only
  }
}

function subscribe(cb: () => void) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

export function acknowledgeTruncation(testId: string) {
  const set = load();
  if (set.has(testId)) return;
  acked = new Set(set).add(testId);
  save(acked);
  for (const l of listeners) l();
}

/** [acknowledged, acknowledge] for one test. */
export function useTruncationAck(testId: string): [boolean, () => void] {
  const isAcked = useSyncExternalStore(
    subscribe,
    () => load().has(testId),
    () => false,
  );
  const ack = useCallback(() => acknowledgeTruncation(testId), [testId]);
  return [isAcked, ack];
}

/** Tests only: forget every acknowledgement. */
export function resetTruncationAckForTests() {
  acked = new Set();
  try {
    window.sessionStorage.removeItem(STORAGE_SLOT);
  } catch {
    // ignore
  }
  for (const l of listeners) l();
}

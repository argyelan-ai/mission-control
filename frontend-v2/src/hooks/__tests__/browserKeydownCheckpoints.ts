/**
 * Test helper: make `window` keydown listeners behave like they do for a REAL
 * key press in a browser.
 *
 * Why this exists: when the browser dispatches a user-initiated event, it runs
 * a microtask checkpoint after EVERY listener callback (HTML spec, "clean up
 * after running script"). React flushes a discrete update (a setState from a
 * keydown listener) in a microtask and runs the passive effects of that sync
 * render right away. So between two window keydown listeners React can
 * re-render and an effect can remove a listener that has not been called yet —
 * per the DOM spec a listener removed during dispatch is then skipped, and a
 * freshly added one is not called either.
 *
 * jsdom (and `fireEvent` / `userEvent`) dispatch synchronously from script,
 * where no checkpoint happens between listeners — that is exactly why a
 * synthetic event closed the dialogs while a real Esc press did not.
 *
 * This helper routes all window keydown listeners through one multiplexer that
 * calls them in registration order, skips listeners removed in the meantime,
 * and drains microtasks between two calls. The first listener still runs
 * inside the real dispatch, so React sees `window.event` and treats the update
 * as discrete, just like in the browser.
 */
export function emulateBrowserKeydownCheckpoints() {
  type Entry = { fn: EventListenerOrEventListenerObject; capture: boolean };
  const registry: Entry[] = [];
  const rawAdd = window.addEventListener;
  const rawRemove = window.removeEventListener;
  const origAdd = rawAdd.bind(window);
  const origRemove = rawRemove.bind(window);
  let pending: Promise<void> = Promise.resolve();

  const captureOf = (opts?: boolean | AddEventListenerOptions | EventListenerOptions) =>
    typeof opts === "boolean" ? opts : Boolean(opts?.capture);

  const call = (fn: EventListenerOrEventListenerObject, event: Event) => {
    if (typeof fn === "function") fn.call(window, event);
    else fn.handleEvent(event);
  };

  const drainMicrotasks = async () => {
    for (let i = 0; i < 20; i++) await Promise.resolve();
  };

  const mux = (event: Event) => {
    const snapshot = [...registry];
    pending = (async () => {
      for (const entry of snapshot) {
        if (!registry.includes(entry)) continue; // removed during dispatch → skipped
        call(entry.fn, event);
        await drainMicrotasks();
      }
    })();
  };
  origAdd("keydown", mux);

  window.addEventListener = ((type: string, fn: EventListenerOrEventListenerObject | null, opts?: boolean | AddEventListenerOptions) => {
    if (type !== "keydown" || !fn) return origAdd(type, fn as EventListenerOrEventListenerObject, opts);
    const capture = captureOf(opts);
    if (!registry.some((e) => e.fn === fn && e.capture === capture)) registry.push({ fn, capture });
  }) as typeof window.addEventListener;

  window.removeEventListener = ((type: string, fn: EventListenerOrEventListenerObject | null, opts?: boolean | EventListenerOptions) => {
    if (type !== "keydown" || !fn) return origRemove(type, fn as EventListenerOrEventListenerObject, opts);
    const capture = captureOf(opts);
    const i = registry.findIndex((e) => e.fn === fn && e.capture === capture);
    if (i >= 0) registry.splice(i, 1);
  }) as typeof window.removeEventListener;

  return {
    /** Resolves once every listener of the last key press has had its turn. */
    settled: () => pending.then(drainMicrotasks),
    restore: () => {
      window.addEventListener = rawAdd;
      window.removeEventListener = rawRemove;
      origRemove("keydown", mux);
      registry.length = 0;
    },
  };
}

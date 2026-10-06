import "@testing-library/jest-dom";
import { vi } from "vitest";
import en from "../messages/en.json";

// jsdom implements no scroll methods at all (not even as a no-op) — several
// components call `el?.scrollIntoView(...)` on an element that DOES exist in
// the test DOM (only the *lookup* is optional-chained, not the method call
// itself), which throws "scrollIntoView is not a function" the moment a full
// page render actually mounts that element and fires the callback. A no-op
// stub matches real browser behavior closely enough for tests that don't
// assert on scroll position.
if (typeof Element !== "undefined" && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoView() {};
}

// Node 25 ships its own global `localStorage`; without `--localstorage-file`
// it is an object without working methods, and it shadows jsdom's Storage.
// zustand's persist middleware captures the storage when lib/store.ts is
// first imported — before any test's beforeEach can stub it — so every
// store write (e.g. the board picker's setBoards) threw "storage.setItem is
// not a function". Page tests only passed while an earlier query happened
// to fail first. A working in-memory Storage here, before any import, fixes
// the whole class; tests that stub localStorage themselves still win.
if (typeof globalThis.localStorage?.setItem !== "function") {
  const mem = new Map<string, string>();
  Object.defineProperty(globalThis, "localStorage", {
    value: {
      get length() {
        return mem.size;
      },
      key: (i: number) => [...mem.keys()][i] ?? null,
      getItem: (k: string) => (mem.has(k) ? (mem.get(k) as string) : null),
      setItem: (k: string, v: string) => void mem.set(k, String(v)),
      removeItem: (k: string) => void mem.delete(k),
      clear: () => mem.clear(),
    },
    configurable: true,
    writable: true,
  });
}

// next-intl global mock: resolves keys against the REAL English catalog, so
// tests keep asserting the actual English labels ("Tasks", "Settings", …)
// without every test having to mount a NextIntlClientProvider. A key that is
// missing from messages/en.json falls back to the key itself — an assertion
// on the label then fails loudly instead of passing on a phantom string.
vi.mock("next-intl", async () => {
  const React = await import("react");
  const resolve = (ns: string | undefined, key: string): string => {
    const path = [...(ns ? ns.split(".") : []), ...key.split(".")];
    let cur: unknown = en;
    for (const p of path) {
      cur = typeof cur === "object" && cur !== null ? (cur as Record<string, unknown>)[p] : undefined;
    }
    return typeof cur === "string" ? cur : key;
  };
  // ICU plural, e.g. "{count, plural, one {# head} other {# heads}}" (review
  // fix round 6, finding 4): a prior version of this mock did not emulate
  // plural/select at all, so a component keying its own t() call on count
  // via a single ICU plural string — instead of an explicit ternary between
  // two catalog keys — got the raw, unformatted ICU template back from every
  // test that rendered it through this mock. Nothing here asserted on that
  // text (grep confirmed no test referenced it), so this was a silent gap,
  // not a passing-on-purpose behaviour; filling it makes those render tests
  // assert the real output instead of an unchecked template string. English
  // plural categories only (`Intl.PluralRules("en")`) — this mock always
  // resolves against the English catalog regardless of the caller's own
  // locale (see `resolve` above), so that's the only grammar it ever needs.
  const BRANCH_RE = /(\w+)\s*\{((?:[^{}]|\{[^{}]*\})*)\}/g;
  const pluralRules = new Intl.PluralRules("en");
  const resolvePlurals = (s: string, values: Record<string, unknown>): string =>
    s.replace(
      /\{(\w+),\s*plural,\s*((?:\w+\s*\{(?:[^{}]|\{[^{}]*\})*\}\s*)+)\}/g,
      (_match, varName: string, branches: string) => {
        const count = Number(values[varName]);
        const map: Record<string, string> = {};
        let bm: RegExpExecArray | null;
        BRANCH_RE.lastIndex = 0;
        while ((bm = BRANCH_RE.exec(branches))) map[bm[1]] = bm[2];
        const category = Number.isFinite(count) ? pluralRules.select(count) : "other";
        const chosen = map[category] ?? map.other ?? "";
        return chosen.replace(/#/g, String(count));
      },
    );
  const interpolate = (s: string, values?: Record<string, unknown>): string => {
    if (values) {
      s = resolvePlurals(s, values);
      for (const [k, v] of Object.entries(values)) {
        if (typeof v !== "function") s = s.split(`{${k}}`).join(String(v));
      }
    }
    return s;
  };
  // Simple {var} interpolation — enough for tests to assert full labels like
  // "Open task: <title>". ICU select is NOT emulated here (only plural,
  // above). t.rich resolves one non-nested level of <tag>chunk</tag> markup
  // against the tag-render functions in `values`.
  const makeT = (ns?: string) => {
    const t = (key: string, values?: Record<string, unknown>) =>
      interpolate(resolve(ns, key), values);
    // Mirrors next-intl's t.has(): true iff the key resolves to a real catalog
    // string (resolve() falls back to the key itself when missing).
    t.has = (key: string) => resolve(ns, key) !== key;
    t.rich = (key: string, values?: Record<string, unknown>) => {
      const s = interpolate(resolve(ns, key), values);
      const nodes: unknown[] = [];
      const re = /<(\w+)>([\s\S]*?)<\/\1>/g;
      let last = 0;
      let m: RegExpExecArray | null;
      let i = 0;
      while ((m = re.exec(s))) {
        if (m.index > last) nodes.push(s.slice(last, m.index));
        const tagFn = values?.[m[1]];
        nodes.push(
          React.createElement(
            React.Fragment,
            { key: i++ },
            (typeof tagFn === "function"
              ? (tagFn as (c: string) => unknown)(m[2])
              : m[2]) as React.ReactNode
          )
        );
        last = m.index + m[0].length;
      }
      nodes.push(s.slice(last));
      return nodes;
    };
    return t;
  };
  return {
    useTranslations: (ns?: string) => makeT(ns),
    useLocale: () => "en",
    NextIntlClientProvider: ({ children }: { children: React.ReactNode }) => children,
  };
});

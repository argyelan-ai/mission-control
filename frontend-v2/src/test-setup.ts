import "@testing-library/jest-dom";
import { vi } from "vitest";
import en from "../messages/en.json";

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
  const interpolate = (s: string, values?: Record<string, unknown>): string => {
    if (values) {
      for (const [k, v] of Object.entries(values)) {
        if (typeof v !== "function") s = s.split(`{${k}}`).join(String(v));
      }
    }
    return s;
  };
  // Simple {var} interpolation — enough for tests to assert full labels like
  // "Open task: <title>". ICU plural/select is NOT emulated here. t.rich
  // resolves one non-nested level of <tag>chunk</tag> markup against the
  // tag-render functions in `values`.
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

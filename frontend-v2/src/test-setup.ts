import "@testing-library/jest-dom";
import { vi } from "vitest";
import en from "../messages/en.json";

// next-intl global mock: resolves keys against the REAL English catalog, so
// tests keep asserting the actual English labels ("Tasks", "Settings", …)
// without every test having to mount a NextIntlClientProvider. A key that is
// missing from messages/en.json falls back to the key itself — an assertion
// on the label then fails loudly instead of passing on a phantom string.
vi.mock("next-intl", () => {
  const resolve = (ns: string | undefined, key: string): string => {
    const path = [...(ns ? ns.split(".") : []), ...key.split(".")];
    let cur: unknown = en;
    for (const p of path) {
      cur = typeof cur === "object" && cur !== null ? (cur as Record<string, unknown>)[p] : undefined;
    }
    return typeof cur === "string" ? cur : key;
  };
  return {
    useTranslations: (ns?: string) => (key: string) => resolve(ns, key),
    useLocale: () => "en",
    NextIntlClientProvider: ({ children }: { children: React.ReactNode }) => children,
  };
});

/**
 * "New agent" speaks one language on every step: rendered with the REAL
 * next-intl and catalogs (the global mock is always English), a German wizard
 * shows none of the English wizard sentences and vice versa.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import de from "../../../../../messages/de.json";
import en from "../../../../../messages/en.json";

vi.unmock("next-intl");
vi.setConfig({ testTimeout: 15_000 });

const { NextIntlClientProvider } = await import("next-intl");
const { AgentWizard } = await import("../AgentWizard");

type Tree = { [k: string]: string | Tree };
function values(t: Tree, p = "", out = new Map<string, string>()): Map<string, string> {
  for (const [k, v] of Object.entries(t)) typeof v === "string" ? out.set(p + k, v) : values(v, `${p}${k}.`, out);
  return out;
}
const wiz = (c: unknown) => values((c as { agents: { wizard: Tree } }).agents.wizard);

// The German text keeps "Harness" in brackets on purpose, as the technical
// name next to the everyday word ("Werkzeug (Harness)").
const KEPT_TERMS = new Set(["Harness"]);

/** The other language's wizard words that differ from this language's own. */
function foreign(own: "de" | "en"): string[] {
  const mine = wiz(own === "de" ? de : en);
  const other = wiz(own === "de" ? en : de);
  const own_ = new Set(mine.values());
  return [...other.entries()]
    .filter(([k, v]) => mine.get(k) !== v && !own_.has(v) && v.length >= 5)
    .map(([, v]) => v.split("{")[0].trim())
    .filter((v) => v.length >= 5 && !KEPT_TERMS.has(v));
}

/** Whole-word match ("Model" must not hit inside "Modell"). */
const hits = (text: string, words: string[]) =>
  words.filter((w) => new RegExp(`(^|[^\\p{L}])${w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}($|[^\\p{L}])`, "u").test(text));

function renderStep(locale: "de" | "en", step: number) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <NextIntlClientProvider locale={locale} messages={locale === "de" ? de : en} onError={() => {}}>
      <QueryClientProvider client={qc}>
        <AgentWizard
          boards={[{ id: "b1", name: "Main" } as never]}
          defaultBoardId="b1"
          initialState={{ step, name: "Nova", scopes: ["tasks:read"], agentRuntime: "cli-bridge" }}
          onClose={() => {}}
          onCreated={() => {}}
        />
      </QueryClientProvider>
    </NextIntlClientProvider>,
  );
}

function shown(root: HTMLElement): string {
  const attrs = [...root.querySelectorAll("[placeholder],[aria-label],[title]")].flatMap((el) =>
    ["placeholder", "aria-label", "title"].map((a) => el.getAttribute(a) ?? ""),
  );
  return [root.textContent ?? "", ...attrs].join(" \n ");
}

describe("New agent wizard — one language per screen", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("{}", { status: 404, headers: { "Content-Type": "application/json" } }),
    );
  });

  for (const step of [0, 1, 2, 3, 4]) {
    it(`step ${step + 1} in German shows no English wizard words`, () => {
      const { baseElement } = renderStep("de", step);
      const text = shown(baseElement);
      expect(text).toContain("Neuer Agent");
      expect(hits(text, foreign("de"))).toEqual([]);
    });
    it(`step ${step + 1} in English shows no German wizard words`, () => {
      const { baseElement } = renderStep("en", step);
      const text = shown(baseElement);
      expect(text).toContain("New agent");
      expect(hits(text, foreign("en"))).toEqual([]);
      expect(text).not.toMatch(/[äöüÄÖÜß]/);
    });
  }

  it("agents.wizard has the same keys in German and English", () => {
    expect([...wiz(de).keys()].sort()).toEqual([...wiz(en).keys()].sort());
  });
});

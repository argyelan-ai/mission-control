/**
 * "New agent" speaks one language on every step: rendered with the REAL
 * next-intl and catalogs (the global mock is always English), a German wizard
 * shows none of the English wizard sentences and vice versa.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
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

  // The readiness rows come from the backend as English ids ("provisioned",
  // "cli-bridge helper", "heartbeat" …). The review step translates them.
  describe("readiness rows after create", () => {
    const READINESS = {
      provision_status: "provisioned",
      runtime: "cli-bridge",
      ready: false,
      checks: [
        { label: "provisioned", ok: true, detail: "provision_status=provisioned" },
        { label: "cli-bridge helper", ok: true, detail: "reachable" },
        { label: "heartbeat", ok: false, detail: "status=offline" },
      ],
    };
    function renderCreated(locale: "de" | "en") {
      vi.spyOn(globalThis, "fetch").mockImplementation(async (url) =>
        String(url).includes("/health-check")
          ? new Response(JSON.stringify(READINESS), { status: 200, headers: { "Content-Type": "application/json" } })
          : new Response("{}", { status: 404, headers: { "Content-Type": "application/json" } }),
      );
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      return render(
        <NextIntlClientProvider locale={locale} messages={locale === "de" ? de : en} onError={() => {}}>
          <QueryClientProvider client={qc}>
            <AgentWizard
              boards={[{ id: "b1", name: "Main" } as never]}
              defaultBoardId="b1"
              initialState={{ step: 4, name: "Nova", scopes: ["tasks:read"], agentRuntime: "cli-bridge", createdAgentId: "a1" }}
              onClose={() => {}}
              onCreated={() => {}}
            />
          </QueryClientProvider>
        </NextIntlClientProvider>,
      );
    }

    it("German: every row German, no backend English", async () => {
      const { baseElement } = renderCreated("de");
      expect(await screen.findByText("Eingerichtet")).toBeInTheDocument();
      const text = baseElement.textContent ?? "";
      expect(text).toContain("Herzschlag");
      for (const raw of ["provisioned", "cli-bridge helper", "reachable", "heartbeat", "status=offline"]) {
        expect(text).not.toContain(raw);
      }
    });

    it("English: plain words, not backend ids", async () => {
      const { baseElement } = renderCreated("en");
      expect(await screen.findByText("Set up")).toBeInTheDocument();
      const text = baseElement.textContent ?? "";
      expect(text).not.toContain("provision_status=");
      expect(text).not.toContain("cli-bridge helper");
    });

    it("an unknown check stays as the backend sends it", async () => {
      vi.spyOn(globalThis, "fetch").mockResolvedValue(
        new Response(JSON.stringify({ ...READINESS, checks: [{ label: "gpu warm", ok: true, detail: "ok" }] }), {
          status: 200, headers: { "Content-Type": "application/json" },
        }),
      );
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      render(
        <NextIntlClientProvider locale="de" messages={de} onError={() => {}}>
          <QueryClientProvider client={qc}>
            <AgentWizard
              boards={[{ id: "b1", name: "Main" } as never]}
              defaultBoardId="b1"
              initialState={{ step: 4, name: "Nova", scopes: [], agentRuntime: "manual", createdAgentId: "a1" }}
              onClose={() => {}}
              onCreated={() => {}}
            />
          </QueryClientProvider>
        </NextIntlClientProvider>,
      );
      expect(await screen.findByText("gpu warm")).toBeInTheDocument();
    });
  });

  it("agents.wizard has the same keys in German and English", () => {
    expect([...wiz(de).keys()].sort()).toEqual([...wiz(en).keys()].sort());
  });
});

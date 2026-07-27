/**
 * ModelCatalogSection — vitest.
 *
 * Coverage:
 *   1. Rendern mit Modellen (Anbieter-Gruppe + Modell-IDs)
 *   2. „neu"-Badge NUR bei bound=false; gebundene Modelle bleiben ruhig
 *   3. new_count als Zähler in der Gruppenkopfzeile
 *   4. „Als Runtime anlegen" → Bestätigung → api.modelCatalog.bind(key, id)
 *   5. Status ok            → kein Hinweisband
 *   6. Status manifest_fallback → Fallback-Hinweis + reason
 *   7. Status credential_missing → Zugangsdaten-Erklärung, nicht als leer getarnt
 *   8. Status unreachable   → „Runtime offline", NICHT „keine Modelle"
 *   9. Leerer Katalog       → expliziter Leerzustand
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ModelCatalogSection } from "../ModelCatalogSection";
import { api } from "@/lib/api";
import type {
  ModelCatalogModel,
  ModelCatalogProvider,
  ModelCatalogResponse,
} from "@/lib/types";

const mkModel = (
  id: string,
  bound = false,
  extra: Partial<ModelCatalogModel> = {},
): ModelCatalogModel => ({ id, display_name: null, bound, ...extra });

const mkProvider = (
  overrides: Partial<ModelCatalogProvider> = {},
): ModelCatalogProvider => {
  const models = overrides.models ?? [
    mkModel("claude-opus-5"),
    mkModel("claude-opus-4-8", true),
  ];
  return {
    key: "anthropic",
    protocol: "anthropic",
    display_name: "Anthropic",
    endpoint: "https://api.anthropic.com",
    status: "ok",
    reason: null,
    cached_at: new Date().toISOString(),
    new_count: models.filter((m) => !m.bound).length,
    ...overrides,
    models,
  };
};

const mkList = (providers: ModelCatalogProvider[]): ModelCatalogResponse => ({ providers });

function renderSection() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ModelCatalogSection />
    </QueryClientProvider>,
  );
}

describe("ModelCatalogSection", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the provider group with its models", async () => {
    vi.spyOn(api.modelCatalog, "list").mockResolvedValue(mkList([mkProvider()]));
    renderSection();

    await waitFor(() => expect(screen.getByText("Anthropic")).toBeInTheDocument());
    expect(screen.getByText("claude-opus-5")).toBeInTheDocument();
    expect(screen.getByText("claude-opus-4-8")).toBeInTheDocument();
  });

  it("marks only unbound models as new and keeps bound models quiet", async () => {
    vi.spyOn(api.modelCatalog, "list").mockResolvedValue(mkList([mkProvider()]));
    renderSection();

    await waitFor(() => expect(screen.getByText("claude-opus-5")).toBeInTheDocument());

    const rows = screen.getAllByTestId("catalog-model-row");
    const newRow = rows.find((r) => r.dataset.bound === "false")!;
    const boundRow = rows.find((r) => r.dataset.bound === "true")!;

    expect(within(newRow).getByTestId("catalog-new-badge")).toHaveTextContent("neu");
    expect(within(newRow).getByRole("button", { name: /Als Runtime anlegen/ })).toBeInTheDocument();

    expect(within(boundRow).queryByTestId("catalog-new-badge")).not.toBeInTheDocument();
    expect(within(boundRow).queryByRole("button", { name: /Als Runtime anlegen/ })).not.toBeInTheDocument();
    expect(within(boundRow).getByText("gebunden")).toBeInTheDocument();
  });

  it("shows the new_count badge in the group header", async () => {
    vi.spyOn(api.modelCatalog, "list").mockResolvedValue(
      mkList([
        mkProvider({
          models: [mkModel("a"), mkModel("b"), mkModel("c", true)],
          new_count: 2,
        }),
      ]),
    );
    renderSection();

    await waitFor(() =>
      expect(screen.getByTestId("catalog-new-count")).toHaveTextContent("2 neu"),
    );
  });

  it("calls api.modelCatalog.bind after confirming 'Als Runtime anlegen'", async () => {
    vi.spyOn(api.modelCatalog, "list").mockResolvedValue(mkList([mkProvider()]));
    const bindSpy = vi
      .spyOn(api.modelCatalog, "bind")
      .mockResolvedValue({ created: true, slug: "claude-opus-5" });

    renderSection();

    const btn = await screen.findByRole("button", { name: /Als Runtime anlegen/ });
    await userEvent.click(btn);

    // B2-ConfirmDialog statt window.confirm
    await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /^Anlegen$/ }));

    await waitFor(() =>
      expect(bindSpy).toHaveBeenCalledWith("anthropic", "claude-opus-5"),
    );
  });

  it("status ok: no honesty banner", async () => {
    vi.spyOn(api.modelCatalog, "list").mockResolvedValue(mkList([mkProvider()]));
    renderSection();

    await waitFor(() => expect(screen.getByText("Anthropic")).toBeInTheDocument());
    expect(screen.queryByTestId("catalog-status-banner")).not.toBeInTheDocument();
  });

  it("status manifest_fallback: says the live probe failed and shows the reason", async () => {
    vi.spyOn(api.modelCatalog, "list").mockResolvedValue(
      mkList([
        mkProvider({
          status: "manifest_fallback",
          reason: "HTTP 500 from /v1/models",
        }),
      ]),
    );
    renderSection();

    const banner = await screen.findByTestId("catalog-status-banner");
    expect(banner).toHaveTextContent(/Live-Abfrage fehlgeschlagen/);
    expect(banner).toHaveTextContent(/bekannte Liste/);
    expect(screen.getByTestId("catalog-status-reason")).toHaveTextContent(
      "HTTP 500 from /v1/models",
    );
  });

  it("status credential_missing: explains the credentials, does not fake an empty list", async () => {
    vi.spyOn(api.modelCatalog, "list").mockResolvedValue(
      mkList([
        mkProvider({
          key: "grok",
          protocol: "grok",
          display_name: "Grok",
          status: "credential_missing",
          reason: "auth.json not mounted into the container",
          models: [],
          new_count: 0,
        }),
      ]),
    );
    renderSection();

    const banner = await screen.findByTestId("catalog-status-banner");
    expect(banner).toHaveTextContent(/Zugangsdaten/);
    expect(banner).toHaveTextContent(/nicht erreichbar/);
    expect(screen.getByTestId("catalog-status-reason")).toHaveTextContent(
      "auth.json not mounted into the container",
    );
    // Keine "keine Modelle"-Aussage, obwohl die Liste leer ist
    expect(screen.queryByText(/meldet aktuell keine Modelle/)).not.toBeInTheDocument();
  });

  it("status unreachable: says 'Runtime offline', never 'keine Modelle'", async () => {
    vi.spyOn(api.modelCatalog, "list").mockResolvedValue(
      mkList([
        mkProvider({
          key: "openai:qwen-general",
          protocol: "openai",
          display_name: "qwen-general",
          status: "unreachable",
          reason: "connection refused: 100.67.20.66:8000",
          models: [],
          new_count: 0,
        }),
      ]),
    );
    renderSection();

    const banner = await screen.findByTestId("catalog-status-banner");
    expect(banner).toHaveTextContent(/Runtime offline/);
    expect(screen.queryByText(/meldet aktuell keine Modelle/)).not.toBeInTheDocument();
    expect(screen.getByTestId("catalog-provider")).toHaveAttribute("data-status", "unreachable");
  });

  it("renders an explicit empty state when no providers are configured", async () => {
    vi.spyOn(api.modelCatalog, "list").mockResolvedValue(mkList([]));
    renderSection();

    await waitFor(() =>
      expect(screen.getByText(/Keine Anbieter konfiguriert/)).toBeInTheDocument(),
    );
  });

  it("renders an error state when the catalog cannot be loaded", async () => {
    vi.spyOn(api.modelCatalog, "list").mockRejectedValue(new Error("API 500: boom"));
    renderSection();

    await waitFor(() =>
      expect(screen.getByText(/konnte nicht geladen werden/)).toBeInTheDocument(),
    );
  });
});

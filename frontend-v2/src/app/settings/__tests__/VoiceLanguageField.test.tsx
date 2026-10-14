import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { VoiceLanguageField } from "../page";
import { api } from "@/lib/api";

// Settings → Profile "Voice language" (operator request 13.10.2026): the
// voice agent's language is a per-user preference, independent of the UI
// locale — "Automatic" keeps today's behaviour (follow the interface
// language). Saved/loaded through the generic per-user preference store
// (GET/PUT /api/v1/settings/{key}), reused rather than a bespoke endpoint.

function renderWithQuery() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <VoiceLanguageField />
    </QueryClientProvider>,
  );
}

describe("Settings → Profile → Voice language", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("defaults to Automatic when nothing was saved yet (404)", async () => {
    vi.spyOn(api.settings, "get").mockRejectedValue(new Error("API 404: Setting not found"));

    renderWithQuery();

    const select = await screen.findByRole("combobox", { name: "Select voice language" });
    expect(select).toHaveValue("auto");
  });

  it("loads a saved German preference", async () => {
    vi.spyOn(api.settings, "get").mockResolvedValue({
      key: "voice_language",
      value: { language: "de" },
    });

    renderWithQuery();

    const select = await screen.findByRole("combobox", { name: "Select voice language" });
    await waitFor(() => expect(select).toHaveValue("de"));
  });

  it("saves the choice through the generic per-user preference store", async () => {
    vi.spyOn(api.settings, "get").mockResolvedValue({ key: "voice_language", value: { language: "auto" } });
    const setSpy = vi.spyOn(api.settings, "set").mockResolvedValue({
      key: "voice_language",
      value: { language: "de" },
    });

    renderWithQuery();

    const select = await screen.findByRole("combobox", { name: "Select voice language" });
    await userEvent.selectOptions(select, "de");

    expect(setSpy).toHaveBeenCalledWith("voice_language", { language: "de" });
    expect(select).toHaveValue("de");
  });

  it("offers Automatic, Deutsch and English", async () => {
    vi.spyOn(api.settings, "get").mockResolvedValue({ key: "voice_language", value: { language: "auto" } });

    renderWithQuery();

    expect(await screen.findByRole("option", { name: /Automatic/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Deutsch" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "English" })).toBeInTheDocument();
  });
});

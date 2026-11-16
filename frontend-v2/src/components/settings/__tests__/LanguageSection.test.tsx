import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { LanguageSection, VoiceLanguageRow } from "../LanguageSection";
import { SECTIONS } from "../sections";
import { api } from "@/lib/api";
import type { Agent } from "@/lib/types";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }),
}));

// Settings → Language (operator decision 16.11.2026, variant A): app and
// voice language moved here from Profile; admins also see the default for new
// agents and every agent's two languages, each opening a sheet.

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const agent = (over: Partial<Agent>): Agent =>
  ({ id: over.name, emoji: "🤖", operator_language: "en", work_language: "en", ...over }) as Agent;

describe("Settings → Language → Voice calls", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("defaults to Automatic when nothing was saved yet (404)", async () => {
    vi.spyOn(api.settings, "get").mockRejectedValue(new Error("API 404: Setting not found"));
    wrap(<VoiceLanguageRow />);
    const select = await screen.findByRole("combobox", { name: "Voice call language" });
    expect(select).toHaveValue("auto");
  });

  it("loads a saved German preference", async () => {
    vi.spyOn(api.settings, "get").mockResolvedValue({ key: "voice_language", value: { language: "de" } });
    wrap(<VoiceLanguageRow />);
    const select = await screen.findByRole("combobox", { name: "Voice call language" });
    await waitFor(() => expect(select).toHaveValue("de"));
  });

  it("saves the choice through the generic per-user preference store", async () => {
    vi.spyOn(api.settings, "get").mockResolvedValue({ key: "voice_language", value: { language: "auto" } });
    const setSpy = vi.spyOn(api.settings, "set").mockResolvedValue({ key: "voice_language", value: { language: "de" } });
    wrap(<VoiceLanguageRow />);
    const select = await screen.findByRole("combobox", { name: "Voice call language" });
    await userEvent.selectOptions(select, "de");
    expect(setSpy).toHaveBeenCalledWith("voice_language", { language: "de" });
    expect(select).toHaveValue("de");
  });

  it("offers Automatic, English and Deutsch", async () => {
    vi.spyOn(api.settings, "get").mockResolvedValue({ key: "voice_language", value: { language: "auto" } });
    wrap(<VoiceLanguageRow />);
    expect(await screen.findByRole("option", { name: "Automatic" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Deutsch" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "English" })).toBeInTheDocument();
  });
});

describe("Settings → Language section", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.settings, "get").mockRejectedValue(new Error("API 404"));
    vi.spyOn(api.settings, "agentLanguage").mockResolvedValue({
      defaults: { operator_language: "en", work_language: "en" },
      supported: ["en", "de"],
    });
    vi.spyOn(api.agents, "list").mockResolvedValue([
      agent({ name: "Backend" }),
      agent({ name: "Lead", operator_language: "de" }),
    ]);
  });

  it("is a registered account section", () => {
    expect(SECTIONS.find((s) => s.id === "language")).toMatchObject({ group: "account" });
    expect(SECTIONS.find((s) => s.id === "language")?.adminOnly).toBeFalsy();
  });

  it("shows only the personal rows to non-admins", async () => {
    wrap(<LanguageSection isAdmin={false} />);
    expect(await screen.findByRole("combobox", { name: "App language" })).toBeInTheDocument();
    expect(screen.queryByText("New agents")).not.toBeInTheDocument();
    expect(api.agents.list).not.toHaveBeenCalled();
  });

  it("lists agents that differ first, the rest as default", async () => {
    wrap(<LanguageSection isAdmin />);
    const rows = await screen.findAllByTestId("language-agent-row");
    expect(rows[0]).toHaveTextContent("Lead");
    expect(rows[0]).toHaveTextContent("German to you · English among agents");
    expect(rows[1]).toHaveTextContent("Backend");
    expect(rows[1]).toHaveTextContent("Default · English");
  });

  it("saves the default for new agents", async () => {
    const put = vi.spyOn(api.settings, "setAgentLanguage").mockResolvedValue({
      defaults: { operator_language: "de", work_language: "en" },
      supported: ["en", "de"],
    });
    wrap(<LanguageSection isAdmin />);
    const select = await screen.findByRole("combobox", { name: "Language for new agents writing to you" });
    await userEvent.selectOptions(select, "de");
    expect(put).toHaveBeenCalledWith({ operator_language: "de" });
  });

  it("opens the sheet for one agent and patches only that field", async () => {
    const update = vi.spyOn(api.agents, "update").mockResolvedValue({} as Agent);
    wrap(<LanguageSection isAdmin />);
    const [lead] = await screen.findAllByTestId("language-agent-row");
    await userEvent.click(lead);
    const sheet = await screen.findByRole("dialog", { name: "Languages of Lead" });
    const among = within(sheet).getByRole("radiogroup", { name: "Among agents" });
    await userEvent.click(within(among).getByRole("radio", { name: "German" }));
    expect(update).toHaveBeenCalledWith("Lead", { work_language: "de" });
    expect(within(sheet).getByRole("link", { name: "Open agent" })).toHaveAttribute("href", "/agents/Lead");
  });
});

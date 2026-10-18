// The Claude card on a German and an English screen, with the REAL next-intl
// and catalogs (the global test mock always resolves English): no English
// sentence on a German screen, and counted agents read grammatically.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ClaudeAccountStatus, ConfigCheckItem } from "@/lib/types";
import de from "../../../../messages/de.json";
import en from "../../../../messages/en.json";

vi.unmock("next-intl");
vi.mock("@/lib/notify", () => ({
  notify: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));
vi.setConfig({ testTimeout: 15_000 });

const { NextIntlClientProvider } = await import("next-intl");
const { ClaudeAccountTab } = await import("../ClaudeAccountTab");

function renderTab(locale: "de" | "en") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <NextIntlClientProvider locale={locale} messages={locale === "de" ? de : en} timeZone="Europe/Zurich">
      <QueryClientProvider client={qc}>
        <ClaudeAccountTab />
      </QueryClientProvider>
    </NextIntlClientProvider>,
  );
}

const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString();

const check = (over: Partial<ConfigCheckItem> = {}): ConfigCheckItem => ({
  kind: "secret",
  subject_id: "claude_code_oauth_token",
  label: "Claude Code OAuth Token",
  secret_key: "claude_code_oauth_token",
  configured: true,
  testable: true,
  cost: "quota",
  state: "ok",
  reason: "ok",
  reason_params: {},
  message: "Claude accepted the key (HTTP 200).",
  duration_ms: 420,
  checked_at: ago(3),
  age_seconds: 180,
  value_changed: false,
  ...over,
});

const status = (over: Partial<ClaudeAccountStatus> = {}): ClaudeAccountStatus => ({
  configured: true,
  env_fallback: false,
  token_updated_at: ago(60 * 24 * 2),
  check: check(),
  claude_agents: 2,
  host_agents: 0,
  stale_agents: [],
  login: { state: "idle", url: null, error: null },
  ...over,
});

const agent = (id: string, busy: boolean) => ({ id, name: `Agent ${id}`, busy, started_at: ago(300) });

// Words that only appear in the English check / error sentences.
const ENGLISH = /\b(the|key|token is|rejected|refuses|expired|revoked|mistyped|again|agents?|restart)\b/i;

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("Claude card — German screen", () => {
  it("a rejected token reads German, the server's English sentence is not shown", async () => {
    vi.spyOn(api.claudeAccount, "status").mockResolvedValue(
      status({
        check: check({
          state: "fail",
          reason: "rejected",
          reason_params: { status: 401 },
          message: "Claude rejected the key (HTTP 401) — expired, revoked or mistyped?",
        }),
      }),
    );
    renderTab("de");
    const line = await screen.findByTestId("claude-check-message");
    expect(line).toHaveTextContent(
      "Claude lehnt den Token ab (HTTP 401) — er ist abgelaufen, widerrufen oder falsch eingegeben. Verbinde neu.",
    );
    expect(line.textContent ?? "").not.toMatch(ENGLISH);
    expect(document.body.textContent).not.toContain("rejected the key");
    expect(screen.queryByTestId("claude-check-detail")).toBeNull();
  });

  it("an unknown reason says so in German; the server text sits in a closed fold", async () => {
    vi.spyOn(api.claudeAccount, "status").mockResolvedValue(
      status({ check: check({ state: "fail", reason: null, reason_params: {}, message: "odd upstream text" }) }),
    );
    renderTab("de");
    const line = await screen.findByTestId("claude-check-message");
    expect(line).toHaveTextContent("Die Prüfung ist fehlgeschlagen. Den Grund kennt MC nicht.");
    const fold = screen.getByTestId("claude-check-detail");
    expect(fold.tagName).toBe("DETAILS");
    expect(fold).not.toHaveAttribute("open");
  });

  it("a login error code reads German", async () => {
    vi.spyOn(api.claudeAccount, "status").mockResolvedValue(
      status({ login: { state: "failed", url: null, error: "code_rejected" } }),
    );
    renderTab("de");
    const error = await screen.findByTestId("claude-login-error");
    expect(error).toHaveTextContent("Claude hat den Code nicht angenommen.");
    expect(error.textContent ?? "").not.toMatch(ENGLISH);
  });

  it("one agent: '1 Agent nutzt …' and 'Neu starten (1 Agent)'", async () => {
    vi.spyOn(api.claudeAccount, "status").mockResolvedValue(status({ stale_agents: [agent("a1", false)] }));
    renderTab("de");
    expect(await screen.findByTestId("claude-stale")).toHaveTextContent("1 Agent nutzt noch den alten Token");
    expect(screen.getByRole("button", { name: "Neu starten (1 Agent)" })).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("1 Agenten");
  });

  it("two agents: '2 Agenten nutzen …' and 'Neu starten (2 Agenten)'", async () => {
    vi.spyOn(api.claudeAccount, "status").mockResolvedValue(
      status({ stale_agents: [agent("a1", false), agent("a2", false)] }),
    );
    renderTab("de");
    expect(await screen.findByTestId("claude-stale")).toHaveTextContent("2 Agenten nutzen noch den alten Token");
    expect(screen.getByRole("button", { name: "Neu starten (2 Agenten)" })).toBeInTheDocument();
  });
});

describe("Claude card — English screen", () => {
  it("one agent and two agents", async () => {
    vi.spyOn(api.claudeAccount, "status").mockResolvedValue(status({ stale_agents: [agent("a1", false)] }));
    const { unmount } = renderTab("en");
    expect(await screen.findByTestId("claude-stale")).toHaveTextContent("1 agent still uses the old token");
    expect(screen.getByRole("button", { name: "Restart 1 agent" })).toBeInTheDocument();
    unmount();

    vi.spyOn(api.claudeAccount, "status").mockResolvedValue(
      status({ stale_agents: [agent("a1", false), agent("a2", false)] }),
    );
    renderTab("en");
    expect(await screen.findByTestId("claude-stale")).toHaveTextContent("2 agents still use the old token");
    expect(screen.getByRole("button", { name: "Restart 2 agents" })).toBeInTheDocument();
  });
});

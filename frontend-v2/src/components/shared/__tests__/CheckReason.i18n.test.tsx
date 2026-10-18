// A check result is worded in the UI language from the backend's reason
// code — never the server's English sentence (Welle 2 fix round, finding 1).
//
// Renders with the REAL next-intl and the real catalogs (the global test
// mock always resolves English), so a German screen is checked for what a
// German operator actually reads.
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import type { ConfigCheckItem } from "@/lib/types";
import { CHECK_REASON_CODES, checkReasonMessage } from "@/lib/checkReason";
import de from "../../../../messages/de.json";
import en from "../../../../messages/en.json";

vi.unmock("next-intl");

const { NextIntlClientProvider } = await import("next-intl");
const { CheckStatus, CheckReason } = await import("../CheckStatus");

function withLocale(locale: "de" | "en", ui: ReactNode) {
  return (
    <NextIntlClientProvider locale={locale} messages={locale === "de" ? de : en} timeZone="Europe/Zurich">
      {ui}
    </NextIntlClientProvider>
  );
}

const ENGLISH_SERVER_TEXT = "Anthropic rejected the key (HTTP 401) — expired, revoked or mistyped?";

function failed(over: Partial<ConfigCheckItem> = {}): ConfigCheckItem {
  return {
    kind: "secret",
    subject_id: "anthropic_api_key",
    label: "Anthropic",
    secret_key: "anthropic_api_key",
    configured: true,
    testable: true,
    cost: "free",
    state: "fail",
    reason: "rejected",
    reason_params: { status: 401 },
    message: ENGLISH_SERVER_TEXT,
    duration_ms: 80,
    checked_at: new Date().toISOString(),
    age_seconds: 5,
    value_changed: false,
    received_at_ms: Date.now(),
    ...over,
  };
}

// Words that only appear in the English sentences of a check result.
const ENGLISH_WORDS =
  /\b(the|key|rejected|expired|revoked|mistyped|failed|tested|works|answered|reason|try|again)\b/i;

describe("check reason in the UI language", () => {
  it("DE: a rejected key reads German — no English sentence anywhere", () => {
    render(withLocale("de", <CheckStatus item={failed()} />));
    const box = screen.getByTestId("check-status");
    expect(box).toHaveTextContent("Fehlgeschlagen");
    expect(screen.getByTestId("check-message")).toHaveTextContent(
      "Abgelehnt (HTTP 401) — der Schlüssel ist abgelaufen, widerrufen oder falsch eingegeben.",
    );
    expect(box.textContent).not.toContain(ENGLISH_SERVER_TEXT);
    expect(box.textContent ?? "").not.toMatch(ENGLISH_WORDS);
    expect(screen.queryByTestId("check-detail")).not.toBeInTheDocument();
  });

  it("EN: the same result reads English", () => {
    render(withLocale("en", <CheckStatus item={failed()} />));
    expect(screen.getByTestId("check-message")).toHaveTextContent(
      "Rejected (HTTP 401) — the key is expired, revoked or mistyped.",
    );
  });

  it("DE: an unknown reason says so in German; the server text only sits in a fold", () => {
    render(
      withLocale("de", <CheckStatus item={failed({ reason: "unknown", reason_params: {}, message: "gh: weird" })} />),
    );
    const line = screen.getByTestId("check-message");
    expect(line).toHaveTextContent("Die Prüfung ist fehlgeschlagen. Den Grund kennt MC nicht.");
    const fold = screen.getByTestId("check-detail");
    expect(fold.tagName).toBe("DETAILS");
    expect(fold).not.toHaveAttribute("open");
    expect(fold).toHaveTextContent("gh: weird");
  });

  it("an old row without a code, or a code the UI does not know, reads as unknown", () => {
    render(withLocale("de", <CheckReason item={failed({ reason: null, message: "old text" })} />));
    expect(screen.getByTestId("check-message")).toHaveAttribute("data-reason", "unknown");
    expect(checkReasonMessage(failed({ reason: "brand_new_code" }))?.code).toBe("unknown");
  });

  it("dimension mismatch carries its numbers", () => {
    render(
      withLocale(
        "de",
        <CheckReason item={failed({ reason: "dimension_mismatch", reason_params: { dimension: 768, expected: 1024 } })} />,
      ),
    );
    expect(screen.getByTestId("check-message")).toHaveTextContent(
      "Antwortet mit 768 statt 1024 Dimensionen.",
    );
  });

  it("every reason code has a German and an English wording", () => {
    const deReasons = (de as { checks: { reason: Record<string, string> } }).checks.reason;
    const enReasons = (en as { checks: { reason: Record<string, string> } }).checks.reason;
    for (const code of CHECK_REASON_CODES) {
      const status = { status: 401, dimension: 3, expected: 4 };
      const msg = checkReasonMessage(failed({ reason: code, reason_params: status, state: code === "ok" ? "ok" : "fail" }));
      expect(msg, code).not.toBeNull();
      expect(deReasons[msg!.key], `de checks.reason.${msg!.key}`).toBeTruthy();
      expect(enReasons[msg!.key], `en checks.reason.${msg!.key}`).toBeTruthy();
      expect(deReasons[msg!.key]).not.toBe(enReasons[msg!.key]);
    }
    expect(Object.keys(deReasons).sort()).toEqual(Object.keys(enReasons).sort());
  });

  it("the UI knows exactly the backend's reason codes", () => {
    const registry = readFileSync(
      path.resolve(__dirname, "../../../../../backend/app/services/config_checks/registry.py"),
      "utf8",
    );
    const block = registry.split("REASON_CODES: tuple[str, ...] = (")[1].split("\n)")[0];
    const backend = [...block.matchAll(/^\s*"([a-z_]+)",/gm)].map((m) => m[1]);
    expect(backend.length).toBeGreaterThan(5);
    expect([...CHECK_REASON_CODES]).toEqual(backend);
  });
});

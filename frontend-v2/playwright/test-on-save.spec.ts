/**
 * Test on save, in a real browser (Welle 2, ADR-093 §2).
 *
 * Saves a key on Settings → API keys and checks that the card says
 * "tested just now" — and still says so after a reload, because the result
 * lives in the check registry, not in the page. Then the setup wizard: an
 * invalid key keeps the wizard on its step with the reason, and
 * "Save & continue" stays off for the same value.
 *
 * Every API call is answered here (a small in-memory registry), so no backend
 * and no real key is involved. Run (from frontend-v2/, dev server on :3001):
 *   npx playwright test playwright/test-on-save.spec.ts
 * Optional: MC_BASE_URL (default http://localhost:3001), MC_BROWSER=webkit.
 */
import { test, expect, type Page } from "@playwright/test";

const BASE = process.env.MC_BASE_URL ?? "http://localhost:3001";
test.use({ browserName: process.env.MC_BROWSER === "webkit" ? "webkit" : "chromium" });
test.setTimeout(120_000);

type Check = {
  kind: string;
  subject_id: string;
  label: string;
  secret_key: string | null;
  configured: boolean;
  testable: boolean;
  cost: string;
  state: "ok" | "fail" | "never_tested";
  reason: string | null;
  reason_params: Record<string, number>;
  message: string | null;
  duration_ms: number | null;
  checked_at: string | null;
  age_seconds: number | null;
  value_changed: boolean;
};

const OPENAI = {
  provider: "openai",
  key: "openai_api_key",
  label: "OpenAI API Key",
  description: "For GPT models",
  placeholder: "sk-...",
};

/** A tiny stand-in for the backend: secrets + the check registry. */
async function fakeBackend(page: Page, opts: { acceptKey: (v: string) => boolean }) {
  const secrets = new Map<string, string>();
  const checks = new Map<string, { ok: boolean; at: number; message: string }>();

  const item = (key: string): Check => {
    const c = checks.get(key);
    return {
      kind: "secret",
      subject_id: key,
      label: key,
      secret_key: key,
      configured: secrets.has(key),
      testable: true,
      cost: key === "claude_code_oauth_token" ? "quota" : "free",
      state: c ? (c.ok ? "ok" : "fail") : "never_tested",
      reason: c ? (c.ok ? "ok" : "rejected") : null,
      reason_params: c && !c.ok ? { status: 401 } : {},
      message: c?.message ?? null,
      duration_ms: c ? 140 : null,
      checked_at: c ? new Date(c.at).toISOString() : null,
      age_seconds: c ? Math.floor((Date.now() - c.at) / 1000) : null,
      value_changed: false,
    };
  };
  const json = (body: unknown, status = 200) => ({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

  await page.route("**/api/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const p = url.pathname;
    const method = req.method();

    if (p === "/api/v1/auth/me")
      return route.fulfill(json({ id: "u1", email: "admin@example.com", name: "Admin", role: "admin" }));
    if (p === "/api/v1/auth/first-run") return route.fulfill(json({ wizard_pending: false }));
    if (p === "/api/v1/secrets/providers")
      return route.fulfill(
        json([
          { provider: "anthropic-claude-code", key: "claude_code_oauth_token", label: "Claude Code OAuth Token", description: "For agents", placeholder: "sk-ant-oat01-..." },
          OPENAI,
        ]),
      );
    if (p === "/api/v1/secrets" && method === "GET")
      return route.fulfill(
        json(
          [...secrets.keys()].map((k) => ({
            id: k, key: k, value_masked: "****" + secrets.get(k)!.slice(-4), provider: null,
            label: k, description: null, created_at: null, updated_at: null,
          })),
        ),
      );
    if (p === "/api/v1/secrets" && method === "POST") {
      const body = req.postDataJSON() as { key: string; value: string };
      secrets.set(body.key, body.value);
      return route.fulfill(json({ id: body.key, key: body.key, value_masked: "****" }));
    }
    const patch = p.match(/^\/api\/v1\/secrets\/([^/]+)$/);
    if (patch && method === "PATCH") {
      secrets.set(decodeURIComponent(patch[1]), (req.postDataJSON() as { value: string }).value);
      return route.fulfill(json({ id: patch[1], key: patch[1], value_masked: "****" }));
    }
    if (p === "/api/v1/config-checks")
      return route.fulfill(json({ kinds: [], cost_hints: {}, items: [...secrets.keys()].map(item) }));
    const run = p.match(/^\/api\/v1\/config-checks\/secret\/([^/]+)\/run$/);
    if (run && method === "POST") {
      const key = decodeURIComponent(run[1]);
      const ok = opts.acceptKey(secrets.get(key) ?? "");
      checks.set(key, {
        ok,
        at: Date.now(),
        message: ok ? "Accepted the key (HTTP 200)." : "Rejected the key (HTTP 401) — expired, revoked or mistyped?",
      });
      return route.fulfill(json(item(key)));
    }
    if (p === "/api/v1/boards") return route.fulfill(json([]));
    // Everything else the shell asks for fails like an offline backend —
    // the app shell copes with that; a made-up empty body would not fit.
    return route.abort();
  });

  await page.addInitScript(() => {
    localStorage.setItem("mc_auth_token", "test-token");
    localStorage.setItem(
      "mc_user",
      JSON.stringify({ id: "u1", email: "admin@example.com", name: "Admin", role: "admin" }),
    );
  });
}

const card = (page: Page) => page.locator(".mc-card", { hasText: "OpenAI API Key" });

test("API keys: save → 'tested just now', and it is still there after a reload", async ({ page }) => {
  await fakeBackend(page, { acceptKey: (v) => v.startsWith("sk-good") });
  await page.goto(`${BASE}/settings?section=apikeys`);

  await card(page).getByRole("button", { name: "Add" }).click();
  await card(page).getByPlaceholder("sk-...").fill("sk-good-123456");
  await card(page).getByRole("button", { name: "Save" }).click();

  const status = card(page).getByTestId("check-status");
  await expect(status).toHaveText(/Works · tested just now/, { timeout: 30_000 });

  await page.reload();
  await expect(card(page).getByTestId("check-status")).toHaveText(/Works · tested just now/, { timeout: 30_000 });
});

test("API keys: a rejected key is red with the reason", async ({ page }) => {
  await fakeBackend(page, { acceptKey: () => false });
  await page.goto(`${BASE}/settings?section=apikeys`);

  await card(page).getByRole("button", { name: "Add" }).click();
  await card(page).getByPlaceholder("sk-...").fill("sk-bad-123456");
  await card(page).getByRole("button", { name: "Save" }).click();

  await expect(card(page).getByTestId("check-status")).toHaveText(/Failed · tested just now/, { timeout: 30_000 });
  await expect(card(page).getByTestId("check-message")).toHaveText(
    "Rejected (HTTP 401) — the key is expired, revoked or mistyped.",
  );
});

test("Setup wizard: an invalid key → no 'continue', a clear reason; a good key goes on", async ({ page }) => {
  await fakeBackend(page, { acceptKey: (v) => v.startsWith("sk-ant-oat01-good") });
  await page.goto(`${BASE}/setup`);

  await page.getByLabel("Key").fill("sk-ant-oat01-bad");
  await page.getByRole("button", { name: /Save & continue/ }).click();

  const keyError = page.getByTestId("setup-key-error");
  await expect(keyError).toContainText("The key did not work.", { timeout: 30_000 });
  await expect(keyError).toContainText("Rejected (HTTP 401) — the key is expired, revoked or mistyped.");
  await expect(page.getByRole("heading", { name: "Connect an LLM provider" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Save & continue/ })).toBeDisabled();

  await page.getByLabel("Key").fill("sk-ant-oat01-good");
  await page.getByRole("button", { name: /Save & continue/ }).click();
  await expect(page.getByRole("heading", { name: "Connect GitHub" })).toBeVisible({ timeout: 30_000 });
});

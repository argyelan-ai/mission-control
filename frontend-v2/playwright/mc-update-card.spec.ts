/**
 * The update card (Settings → About, update plan PR 5) in a real browser.
 *
 * Every API call is answered here — no backend, no machine, no real update.
 * Each scenario fakes one state of the self-update and checks the card shows
 * it; with MC_SHOTS=<dir> every state is also photographed at 390 px (dark
 * and light, German and English) and on the desktop.
 *
 * Run (from frontend-v2/, dev server with the API proxy pointing nowhere):
 *   API_PROXY_TARGET=http://127.0.0.1:9 npm run dev
 *   npx playwright test playwright/mc-update-card.spec.ts
 * Optional: MC_BASE_URL (default http://localhost:3001), MC_SHOTS=<dir>,
 * MC_BROWSER=webkit, MC_SLOW=1 (adds the 60-second offline probe).
 */
import { test, expect, type Page, type Route } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { join } from "node:path";

const BASE = process.env.MC_BASE_URL ?? "http://localhost:3001";
const SHOTS = process.env.MC_SHOTS ?? "";
test.use({ browserName: process.env.MC_BROWSER === "webkit" ? "webkit" : "chromium" });
test.setTimeout(180_000);

const MIN = 60_000;
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();
const RUN_ID = "20261019T115000Z-abc123";

type Json = Record<string, unknown>;

function run(over: Json = {}): Json {
  return {
    run_id: RUN_ID,
    phase: "pull",
    phase_state: "intent",
    status: "running",
    source: "ui",
    from: { tag: "v0.9.2" },
    target: { tag: "v0.9.3" },
    running: true,
    interrupted: false,
    created_at: ago(6 * MIN),
    updated_at: ago(10_000),
    finished_at: null,
    error: null,
    failure_kind: null,
    backup: null,
    events: [],
    bridge: { api_version: 1, boot: {}, starts: { used: 1, limit: 3 } },
    ...over,
  };
}

function status(r: Json | null, over: Json = {}): Json {
  return {
    drain: { active: !!r, run_id: r ? RUN_ID : null, expires_at: null },
    running_work: { tasks: 0, group_rounds: 0 },
    bridge_reachable: true,
    stale: false,
    run: r,
    ...over,
  };
}

const EVENTS_TO_VERIFY = [
  { at: ago(6 * MIN), phase: "preflight", event: "done", detail: "checks passed" },
  { at: ago(5 * MIN), phase: "drain", event: "done", detail: "quiet" },
  { at: ago(4 * MIN), phase: "pull", event: "done", detail: "backend frontend" },
  { at: ago(3 * MIN), phase: "backup", event: "done", detail: "mc_backup_preupdate_20261019_115700.sql.gz" },
  { at: ago(2 * MIN), phase: "switch", event: "done", detail: "v0.9.3" },
  { at: ago(MIN), phase: "up", event: "done", detail: "backend mc-worker frontend" },
  { at: ago(50_000), phase: "verify", event: "intent", detail: "four checks" },
];

const HISTORY = {
  runs: [
    {
      run_id: "20261003T090000Z-111111", source: "ui", from_version: "v0.9.1", target_version: "v0.9.2",
      phase: "done", error: null, failure_kind: null, backup: "mc_backup_preupdate_20261003_090200.sql.gz",
      started_by: "Admin", started_at: ago(16 * 24 * 60 * MIN), finished_at: ago(16 * 24 * 60 * MIN - 6 * MIN),
      rollback_requested_by: null, rollback_requested_at: null, drain_lifted_at: null,
    },
    {
      run_id: "20260920T090000Z-222222", source: "terminal", from_version: "v0.9.0", target_version: "v0.9.1",
      phase: "rolled_back", error: "backend did not become healthy", failure_kind: null, backup: null,
      started_by: null, started_at: ago(29 * 24 * 60 * MIN), finished_at: ago(29 * 24 * 60 * MIN - 9 * MIN),
      rollback_requested_by: null, rollback_requested_at: null, drain_lifted_at: null,
    },
  ],
};

const PREFLIGHT_OK = {
  current: "v0.9.2", target: "v0.9.3", up_to_date: false,
  bridge: { reachable: true, api_version: 1 },
  checks: [
    { id: "bridge", status: "ok", message: "update door of the cli-bridge answers (api 1)" },
    { id: "linger", status: "ok", message: "linger on" },
    { id: "channel", status: "ok", message: "channel stable" },
    { id: "release", status: "ok", message: "v0.9.2 → v0.9.3" },
    { id: "disk", status: "ok", message: "41000 MB free" },
    { id: "work", status: "warn", message: "1 task(s) or group-chat round(s) in progress — the update waits until they are done" },
    { id: "step_up", status: "ok", message: "you can confirm the update with your password" },
    { id: "schedules", status: "ok", message: "no scheduled job due within 15 minutes" },
  ],
  ok: true,
  running_work: { tasks: 1, group_rounds: 0 },
  drain: { active: false, run_id: null, expires_at: null },
};

const PREFLIGHT_BLOCKED = {
  ...PREFLIGHT_OK,
  ok: false,
  checks: [
    { id: "disk", status: "fail", message: "1900 MB free, 3000 MB needed for images and the backup" },
    { id: "schedules", status: "fail", message: "1 scheduled job(s) due within 15 minutes (Nightly report) — update after they ran, or snooze them" },
    { id: "bridge", status: "ok", message: "update door of the cli-bridge answers (api 1)" },
    { id: "linger", status: "fail", message: "without linger the bridge stops at logout — run on the machine: sudo loginctl enable-linger mc", command: "sudo loginctl enable-linger mc" },
    { id: "release", status: "ok", message: "v0.9.2 → v0.9.3" },
    { id: "step_up", status: "ok", message: "you can confirm the update with your password" },
  ],
};

interface Scenario {
  name: string;
  state: string;
  status?: Json | "offline";
  managed?: boolean;
  disabled?: boolean;
  version?: Json;
  preflight?: Json | "slow";
  /** Steps in the page before the picture. */
  act?: (page: Page) => Promise<void>;
}

const VERSION = { current: "0.9.2", latest: "v0.9.3", release_url: "https://example.invalid/releases/v0.9.3", update_available: true };
const DONE_VERSION = { current: "0.9.3", latest: "v0.9.3", release_url: null, update_available: false };

const SCENARIOS: Scenario[] = [
  { name: "01-available", state: "available", status: status(null) },
  {
    name: "02-checking",
    state: "checking",
    status: status(null),
    preflight: "slow",
    act: async (p) => p.getByTestId("update-check").click(),
  },
  {
    name: "03-ready",
    state: "ready",
    status: status(null),
    preflight: PREFLIGHT_OK,
    act: async (p) => p.getByTestId("update-check").click(),
  },
  {
    name: "04-blocked",
    state: "blocked",
    status: status(null),
    preflight: PREFLIGHT_BLOCKED,
    act: async (p) => p.getByTestId("update-check").click(),
  },
  {
    name: "05-sheet",
    state: "ready",
    status: status(null),
    preflight: PREFLIGHT_OK,
    act: async (p) => {
      await p.getByTestId("update-check").click();
      await p.getByTestId("update-now").click();
      await p.getByTestId("update-sheet-password").fill("correct horse");
    },
  },
  { name: "06-waiting", state: "waiting", status: status(run({ phase: "drain" }), { running_work: { tasks: 2, group_rounds: 1 } }) },
  { name: "07-running", state: "running", status: status(run({ phase: "backup", events: EVENTS_TO_VERIFY.slice(0, 3) })) },
  { name: "08-restarting", state: "restarting", status: "offline" },
  {
    name: "09-success",
    state: "success",
    version: DONE_VERSION,
    status: status(run({ phase: "done", status: "done", running: false, finished_at: ago(30_000), events: EVENTS_TO_VERIFY })),
    act: async (p) => p.getByRole("button", { name: /History|Verlauf/ }).click(),
  },
  {
    name: "10-rolled-back",
    state: "rolledBack",
    status: status(
      run({
        phase: "rolled_back", running: false, finished_at: ago(30_000), error: "backend did not become healthy within 12 min",
        events: [...EVENTS_TO_VERIFY.slice(-3), { at: ago(20_000), phase: "rolling_back", event: "intent", detail: "verify failed" }, { at: ago(5000), phase: "rolled_back", event: "done", detail: "back on v0.9.2" }],
      }),
    ),
  },
  { name: "11-interrupted", state: "interrupted", status: status(run({ phase: "up", running: false, interrupted: true }), { command: "./install.sh --resume" }) },
  {
    name: "12-interrupted-no-bridge",
    state: "interrupted",
    status: status(run({ phase: "up", running: false }), { stale: true, bridge_reachable: false, command: "./install.sh --resume" }),
  },
  {
    name: "13-rollback-failed",
    state: "rollbackFailed",
    status: status(
      run({
        phase: "rollback_failed", running: false, finished_at: ago(MIN), error: "verify failed — the way back failed: restore exited 1",
        events: [{ phase: "verify", event: "intent" }, { phase: "rolling_back", event: "intent", detail: "verify failed" }, { phase: "rollback_failed", event: "done", detail: "restore exited 1" }],
      }),
      { command: "./install.sh --rollback" },
    ),
  },
  { name: "14-aborted-download", state: "aborted", status: status(run({ phase: "aborted", running: false, failure_kind: "network", finished_at: ago(MIN), error: "pull failed: TLS handshake timeout" })) },
  { name: "15-managed", state: "managed", managed: true },
  { name: "16-up-to-date", state: "upToDate", version: DONE_VERSION, status: status(null) },
];

async function fakeBackend(page: Page, sc: Scenario, locale: string) {
  const json = (route: Route, body: unknown, s = 200) =>
    route.fulfill({ status: s, contentType: "application/json", body: JSON.stringify(body) });
  let statusCalls = 0;
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const p = url.pathname;
    const method = route.request().method();
    if (p === "/api/v1/auth/me") return json(route, { id: "u1", email: "admin@example.invalid", name: "Admin", role: "admin" });
    if (p === "/api/v1/auth/first-run") return json(route, { wizard_pending: false });
    if (p === "/api/v1/system/version") return json(route, sc.version ?? VERSION);
    if (p === "/api/v1/auth/step-up" && method === "GET") return json(route, { available: true, reason: null, ttl_seconds: 300 });
    if (p === "/api/v1/system/update/status") {
      statusCalls += 1;
      if (sc.managed) return json(route, { detail: { code: "update_managed", message: "managed", managed: true, enabled: true } }, 409);
      if (sc.disabled) return json(route, { detail: { code: "update_disabled", message: "off" } }, 404);
      if (sc.status === "offline") {
        // First a running update, then MC goes away (restart gap).
        if (statusCalls <= 1) return json(route, status(run({ phase: "up", events: EVENTS_TO_VERIFY.slice(0, 6) })));
        return route.abort("connectionrefused");
      }
      return json(route, sc.status);
    }
    if (p === "/api/v1/system/update/history") return json(route, HISTORY);
    if (p === "/api/v1/system/update/preflight") {
      if (sc.preflight === "slow") return; // never answers — "checking" stays on screen
      return json(route, sc.preflight ?? PREFLIGHT_OK);
    }
    if (p === "/api/v1/boards") return json(route, []);
    return route.abort();
  });
  await page.context().addCookies([{ name: "NEXT_LOCALE", value: locale, url: BASE }]);
}

async function open(page: Page, sc: Scenario, opts: { theme: "dark" | "light"; locale: "de" | "en" }) {
  await fakeBackend(page, sc, opts.locale);
  await page.addInitScript(
    ([theme]) => {
      localStorage.setItem("mc_auth_token", "test-token");
      localStorage.setItem("mc_user", JSON.stringify({ id: "u1", email: "admin@example.invalid", name: "Admin", role: "admin" }));
      localStorage.setItem("mc_theme", theme);
    },
    [opts.theme],
  );
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`${BASE}/settings?section=about`);
  const card = page.getByTestId("update-card");
  if (sc.disabled) return card;
  await expect(card).toBeVisible({ timeout: 60_000 });
  if (sc.act) await sc.act(page);
  await expect(card).toHaveAttribute("data-state", sc.state, { timeout: 30_000 });
  return card;
}

async function shoot(page: Page, name: string, firstScreen = false) {
  if (!SHOTS) return;
  mkdirSync(SHOTS, { recursive: true });
  await page.waitForTimeout(400);
  const sheet = page.getByTestId("update-sheet");
  if (await sheet.isVisible().catch(() => false)) {
    await page.screenshot({ path: join(SHOTS, `${name}.png`) });
    return;
  }
  // The card on its own (the page scrolls inside a container, so a
  // full-page picture would cut it) …
  // A tall card is photographed in a tall window, so nothing overlays it.
  const vp = page.viewportSize();
  if (vp) await page.setViewportSize({ width: vp.width, height: 2400 });
  await page.waitForTimeout(200);
  await page.getByTestId("update-card").screenshot({ path: join(SHOTS, `${name}.png`) });
  if (vp) await page.setViewportSize(vp);
  // … and the first screen as the operator opens the section.
  if (firstScreen) {
    await page.evaluate(() => document.querySelectorAll("*").forEach((el) => el.scrollTop > 0 && (el.scrollTop = 0)));
    await page.waitForTimeout(200);
    await page.screenshot({ path: join(SHOTS, `${name}-screen.png`) });
  }
}

// ── every state, phone (390 px) ─────────────────────────────────────────────

for (const sc of SCENARIOS) {
  test(`phone · ${sc.name} → ${sc.state}`, async ({ browser }) => {
    const combos = SHOTS
      ? ([["dark", "de"], ["light", "de"], ["dark", "en"], ["light", "en"]] as const)
      : ([["dark", "en"]] as const);
    for (const [theme, locale] of combos) {
      const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
      const page = await ctx.newPage();
      await open(page, sc, { theme, locale });
      // Nothing wider than the phone (no sideways scroll).
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow).toBeLessThanOrEqual(0);
      await shoot(page, `phone-${theme}-${locale}-${sc.name}`, theme === "dark" && locale === "de");
      await ctx.close();
    }
  });
}

// ── desktop ─────────────────────────────────────────────────────────────────

test("desktop · every state (dark DE, light EN)", async ({ browser }) => {
  for (const sc of SCENARIOS) {
    for (const [theme, locale] of [["dark", "de"], ["light", "en"]] as const) {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      const page = await ctx.newPage();
      await open(page, sc, { theme, locale });
      await shoot(page, `desktop-${theme}-${locale}-${sc.name}`, true);
      await ctx.close();
    }
  }
});

// ── switch off: no card ─────────────────────────────────────────────────────

test("switch off (404) → no card, About as before", async ({ page }) => {
  await open(page, { name: "off", state: "", disabled: true, status: status(null) }, { theme: "dark", locale: "en" });
  await expect(page.getByText("Update available: v0.9.3")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("update-card")).toHaveCount(0);
});

// ── sabotage probe (a): the status API offline for 60 s ─────────────────────

test("status API offline for 60 s → the page waits, never 'failed', then success", async ({ page }) => {
  test.skip(!process.env.MC_SLOW, "set MC_SLOW=1 for the 60-second probe");
  let offline = false;
  let done = false;
  await fakeBackend(page, { name: "x", state: "", status: status(run({ phase: "up" })) }, "en");
  await page.route("**/api/v1/system/update/status", (route) => {
    if (offline) return route.abort("connectionrefused");
    const body = done
      ? status(run({ phase: "done", running: false, finished_at: new Date().toISOString(), events: EVENTS_TO_VERIFY }))
      : status(run({ phase: "up" }));
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.addInitScript(() => {
    localStorage.setItem("mc_auth_token", "test-token");
    localStorage.setItem("mc_user", JSON.stringify({ id: "u1", email: "admin@example.invalid", name: "Admin", role: "admin" }));
  });
  await page.goto(`${BASE}/settings?section=about`);
  const card = page.getByTestId("update-card");
  await expect(card).toHaveAttribute("data-state", "running", { timeout: 60_000 });
  offline = true;
  await expect(card).toHaveAttribute("data-state", "restarting", { timeout: 30_000 });
  const until = Date.now() + 60_000;
  while (Date.now() < until) {
    await expect(card).toHaveAttribute("data-state", "restarting");
    await expect(card).not.toContainText(/failed|error/i);
    await page.waitForTimeout(5000);
  }
  done = true;
  offline = false;
  await expect(card).toHaveAttribute("data-state", "success", { timeout: 30_000 });
});

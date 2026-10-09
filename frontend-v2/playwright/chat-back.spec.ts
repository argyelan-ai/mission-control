/**
 * Back from an open chat lands on the chats list — on the phone, in a real
 * browser.
 *
 * The phone's edge swipe is the browser's history back. A unit test with a
 * mocked router cannot prove what that does: an earlier fix (window.history
 * .replaceState) was green in jsdom and had no effect live. So this drives
 * the dev server in WebKit (Chromium if WebKit is not installed) at iPhone 15
 * size, opens a chat the way the operator does, and calls page.goBack().
 *
 * Run (from frontend-v2/, dev server on :3001 with the API proxy):
 *   MC_JWT=<token> npx playwright test playwright/chat-back.spec.ts
 * Optional: MC_BASE_URL (default http://localhost:3001), MC_BROWSER=chromium.
 *
 * Read-only against the backend: every non-GET API request is aborted.
 */
import { test, expect, devices, type Page } from "@playwright/test";

const BASE = process.env.MC_BASE_URL ?? "http://localhost:3001";
const TOKEN = process.env.MC_JWT ?? "";
const { defaultBrowserType: _ignored, ...iphone } = devices["iPhone 15"];

test.skip(!TOKEN, "MC_JWT is not set — without a token the app only shows the login");
test.use({ ...iphone, browserName: process.env.MC_BROWSER === "chromium" ? "chromium" : "webkit" });
test.setTimeout(120_000);

type Ids = { agentId: string; agentName: string; hasHead: boolean };
let ids: Ids;

async function prepare(page: Page) {
  await page.route("**/api/**", (route) =>
    route.request().method() === "GET" ? route.continue() : route.abort(),
  );
  await page.addInitScript(
    ([token, agentId]) => {
      localStorage.setItem("mc_auth_token", token);
      // The Chats tab and the ⊕ "continue" chip point at the last chat.
      if (agentId) localStorage.setItem("mc-recent-chats", JSON.stringify([{ kind: "agent", id: agentId }]));
    },
    [TOKEN, ids?.agentId ?? ""],
  );
}

async function api<T>(page: Page, path: string): Promise<T> {
  return page.evaluate(async (p) => {
    const r = await fetch(p, { headers: { Authorization: "Bearer " + localStorage.getItem("mc_auth_token") } });
    return r.json();
  }, path);
}

const list = (page: Page) => page.getByTestId("session-list-mobile");
const chat = (page: Page) => page.getByTestId("chat-column");

/** A chat is on screen and its URL has settled (the entry marker is gone). */
async function expectChatOpen(page: Page) {
  await expect(chat(page)).toBeVisible({ timeout: 30_000 });
  await expect(list(page)).toBeHidden();
  await expect(page).toHaveURL(
    (u) =>
      u.pathname === "/sessions" &&
      !u.searchParams.has("enter") &&
      ["agent", "group", "head"].some((k) => u.searchParams.has(k)),
  );
}

/** The chats list, no chat open, and the URL names no chat. */
async function expectChatsList(page: Page) {
  await expect(page).toHaveURL((u) => u.pathname === "/sessions" && u.search === "", { timeout: 15_000 });
  await expect(list(page)).toBeVisible();
  await expect(chat(page)).toBeHidden();
}

async function goHome(page: Page) {
  await page.goto(`${BASE}/`, { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("mobile-tab-bar")).toBeVisible({ timeout: 60_000 });
}

test.beforeAll(async ({ browser }) => {
  const ctx = await browser.newContext({ ...iphone });
  const page = await ctx.newPage();
  ids = { agentId: "", agentName: "", hasHead: false };
  await prepare(page);
  await page.goto(`${BASE}/login`, { waitUntil: "domcontentloaded" });
  const docker = await api<{ id: string }[]>(page, "/api/v1/docker-sessions/agents");
  const host = await api<{ id: string }[]>(page, "/api/v1/host-sessions/agents");
  const agent = [...docker, ...host][0] as { id: string; name: string } | undefined;
  if (!agent) throw new Error("the API lists no agent — nothing to open");
  const heads = await api<{ runs?: unknown[] }>(page, "/api/v1/heads?recent_days=30");
  ids = { agentId: agent.id, agentName: agent.name, hasHead: (heads.runs?.length ?? 0) > 0 };
  await ctx.close();
});

test.beforeEach(async ({ page }) => {
  await prepare(page);
});

test("Chats tab from Home: back shows the chats list, back again goes Home", async ({ page }) => {
  await goHome(page);
  await page.locator('[data-tab="chats"]').click();
  await expectChatOpen(page);

  await page.goBack();
  await expectChatsList(page);

  await page.goBack();
  await expect(page).toHaveURL(`${BASE}/`);
});

test("⊕ sheet 'continue' chip from Home: back shows the chats list", async ({ page }) => {
  await goHome(page);
  await page.locator('[data-tab="new"]').click();
  await page.getByTestId("quick-last-chat").click();
  await expectChatOpen(page);

  await page.goBack();
  await expectChatsList(page);

  await page.goBack();
  await expect(page).toHaveURL(`${BASE}/`);
});

test("Agents page 'open session': back shows the chats list, back again returns to Agents", async ({ page }) => {
  await goHome(page);
  await page.goto(`${BASE}/agents`, { waitUntil: "domcontentloaded" });
  await page.locator(`a[href*="agent=${ids.agentId}"]:visible`).first().click();
  await expectChatOpen(page);

  await page.goBack();
  await expectChatsList(page);

  await page.goBack();
  await expect(page).toHaveURL(`${BASE}/agents`);
});

test("Inbox head question: back shows the chats list, back again returns to the Inbox", async ({ page }) => {
  test.skip(!ids.hasHead, "the API lists no head run in the last 30 days");
  // Fixture on top of the live data: every head run reads as waiting on the
  // operator, so the Inbox shows its "open question" link to the head chat.
  const asQuestion = (run: Record<string, unknown>) => ({ ...run, state: "needs_you", task_deleted: false });
  await page.route(
    // The run list and one run — not its transcript, log or occupancy.
    (u) => u.pathname === "/api/v1/heads" || /^\/api\/v1\/heads\/[0-9a-f-]{36}$/.test(u.pathname),
    async (route) => {
      if (route.request().method() !== "GET") return route.abort();
      const res = await route.fetch();
      const body = await res.json().catch(() => null);
      if (body && Array.isArray(body.runs)) body.runs = body.runs.map(asQuestion);
      else if (body && typeof body.run_id === "string") Object.assign(body, asQuestion(body));
      await route.fulfill({ response: res, json: body });
    },
  );
  await goHome(page);
  await page.locator('[data-tab="inbox"]').click();
  const link = page.getByTestId("inbox-head-question").first();
  await expect(link).toBeVisible({ timeout: 30_000 });
  await link.click();
  await expectChatOpen(page);

  await page.goBack();
  await expectChatsList(page);

  await page.goBack();
  await expect(page).toHaveURL(`${BASE}/inbox`);
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

test("a deep link loaded straight into the tab: back shows the chats list", async ({ page }) => {
  await goHome(page);
  await page.goto(`${BASE}/sessions?agent=${ids.agentId}`, { waitUntil: "domcontentloaded" });
  await expectChatOpen(page);

  await page.goBack();
  await expectChatsList(page);

  await page.goBack();
  await expect(page).toHaveURL(`${BASE}/`);
});

test("in-app back and a chat opened from the list behave like the swipe — no loop, no double list", async ({ page }) => {
  await goHome(page);
  await page.locator('[data-tab="chats"]').click();
  await expectChatOpen(page);

  // The chat header's back chevron: same place as the swipe.
  await page.getByRole("button", { name: "Back to chats" }).click();
  await expectChatsList(page);

  // Open a chat from the list: back must return to the list …
  await list(page).getByRole("option", { name: new RegExp(ids.agentName) }).first().click();
  await expectChatOpen(page);
  await page.goBack();
  await expectChatsList(page);

  // … and back from the list leaves the chats page in one step.
  await page.goBack();
  await expect(page).toHaveURL(`${BASE}/`);
});

test("reloading an open chat adds no second list entry", async ({ page }) => {
  await goHome(page);
  await page.locator('[data-tab="chats"]').click();
  await expectChatOpen(page);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expectChatOpen(page);

  await page.goBack();
  await expectChatsList(page);

  await page.goBack();
  await expect(page).toHaveURL(`${BASE}/`);
});

test("returning to a chat with back from another page adds no extra entries", async ({ page }) => {
  await goHome(page);
  await page.locator('[data-tab="chats"]').click();
  await expectChatOpen(page);
  // Leave the chat for another page, then come back with the browser.
  await page.goto(`${BASE}/agents`, { waitUntil: "domcontentloaded" });
  await page.goBack();
  await expectChatOpen(page);

  await page.goBack();
  await expectChatsList(page);

  await page.goBack();
  await expect(page).toHaveURL(`${BASE}/`);
});

test("⊕ chip while the chats list is open: back returns to the list once, then Home", async ({ page }) => {
  await goHome(page);
  await page.locator('[data-tab="chats"]').click();
  await expectChatOpen(page);
  await page.getByRole("button", { name: "Back to chats" }).click();
  await expectChatsList(page);

  await page.locator('[data-tab="new"]').click();
  await page.getByTestId("quick-last-chat").click();
  await expectChatOpen(page);

  await page.goBack();
  await expectChatsList(page);
  await page.goBack();
  await expect(page).toHaveURL(`${BASE}/`);
});

test("forward after back reopens the chat", async ({ page }) => {
  await goHome(page);
  await page.locator('[data-tab="chats"]').click();
  await expectChatOpen(page);
  await page.goBack();
  await expectChatsList(page);

  await page.goForward();
  await expectChatOpen(page);
  await page.goBack();
  await expectChatsList(page);
});

test.describe("desktop", () => {
  test.use({ viewport: { width: 1440, height: 900 }, isMobile: false, hasTouch: false });

  test("desktop keeps one entry per chat link: back from the chat leaves the chats page", async ({ page }) => {
    await page.goto(`${BASE}/agents`, { waitUntil: "domcontentloaded" });
    await page.locator(`a[href*="agent=${ids.agentId}"]:visible`).first().click();
    await expect(page.getByTestId("chat-column")).toBeVisible({ timeout: 30_000 });
    await expect(page).toHaveURL((u) => u.searchParams.get("agent") === ids.agentId);

    await page.goBack();
    await expect(page).toHaveURL(`${BASE}/agents`);
  });
});

// J-first-run (docs/produkt/landkarte.yaml): "When I have just installed MC,
// I want a working agent on my own model that asks me before it commits —
// and I answer it from my phone."
//
// The onboarding flow S0–S10 (docs/journeys.md, "First run") on today's
// product, at phone width, from a fresh stack to the effect at the agent.
// The model is the stub model (e2e/stub-model), the agent the fake harness —
// no provider keys, no network, no real CLI.
//
// Where a step is not built yet the test takes today's path and says so; the
// step's label names the PR that replaces it, so the later PR swaps one step
// instead of writing a second journey:
//   S0   install        the stack itself (run-journeys.sh builds and starts it);
//                       the installer's "port busy" line is the workflow's
//                       install-preflight job + test_install_port_preflight.py
//   S3–S4 first agent   API fixture until "first-run" creates the lead, the
//                       practice repo (a registered repo row) and the first
//                       job (Welle 3 PR 3-1)
//   S6–S8 phone pairing not built (Welle 4); the test is on the phone already
//   S9   question card  the Inbox card (two taps) until the chat card (3-3)
import { expect, test, type Page } from "@playwright/test";
import { FakeAgent } from "./lib/fakeHarness";
import { api, createBoard, createTask, getTask, operator, operatorToken, setupRequired, signIn } from "./lib/mc";
import { withServiceDown } from "./lib/stack";

test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

// The stub model, as the backend reaches it inside the stack.
const STUB_URL = "http://stub-model:8080/v1";
const STUB_MODEL = "stub-model";
const STUB_NAME = "OpenAI-compatible @ stub-model:8080"; // the add dialog's suggested name

const run = Date.now().toString(36);
const re = (text: string) => new RegExp(text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));

type RuntimeRow = { id: string; slug: string; endpoint: string | null; model_identifier: string | null };

async function stubRuntime(token: string): Promise<RuntimeRow | undefined> {
  const { runtimes: rows } = await api<{ runtimes: RuntimeRow[] }>("GET", "/runtimes", token);
  return rows.find((r) => r.endpoint?.replace(/\/+$/, "") === STUB_URL);
}

/** Runtimes → "Connect by address": paste the URL and probe it. */
async function probeAddress(page: Page, url: string) {
  await page.goto("/runtimes");
  await page.getByRole("button", { name: "Connect by address" }).click();
  const dialog = page.getByRole("dialog", { name: "Add runtime" });
  await dialog.getByLabel("Endpoint URL").fill(url);
  await dialog.getByRole("button", { name: "Probe" }).click();
  return dialog;
}

/** Runtimes → Infrastructure → the runtime's panel → "Re-probe model". */
async function reprobe(page: Page, name: string) {
  await page.goto("/runtimes");
  await page.getByRole("tab", { name: "Infrastructure" }).click();
  await page.getByRole("button", { name: re(name) }).first().click();
  const panel = page.getByRole("dialog", { name });
  await panel.getByRole("button", { name: "Re-probe model" }).click();
  return panel;
}

test("S1–S10: a fresh install gets an agent on its own model that asks before it commits, answered from the phone", async ({
  page,
}) => {
  test.setTimeout(240_000);
  const account = operator();
  await page.context().addCookies([{ name: "NEXT_LOCALE", value: "en", url: test.info().project.use.baseURL! }]);

  await test.step("S1 account: the first visit creates the admin and lands in setup", async () => {
    // A stack kept up with --keep, or a --grep re-run, already has the admin:
    // then the same page signs in instead.
    const fresh = await setupRequired();
    await page.goto("/");
    await expect(page).toHaveURL(/\/login/);
    if (fresh) {
      await expect(page.getByText("Create the first admin account")).toBeVisible();
      await page.getByLabel("Name").fill(account.name);
      await page.getByLabel("Email").fill(account.email);
      await page.getByLabel("Password", { exact: true }).fill(account.password);
      await page.getByLabel("Repeat password").fill(account.password);
      await page.getByRole("button", { name: "Create admin" }).click();
    } else {
      test.info().annotations.push({ type: "note", description: "stack not fresh: S1 signed in instead of registering" });
      await page.getByLabel("Email").fill(account.email);
      await page.getByLabel("Password", { exact: true }).fill(account.password);
      await page.getByRole("button", { name: "Sign in" }).click();
      await expect(page).not.toHaveURL(/\/login/);
      // The wizard may be done already; /setup stays reachable.
      if (!/\/setup/.test(page.url())) await page.goto("/setup");
    }
    await expect(page).toHaveURL(/\/setup/);
  });

  await test.step("S2 AI: no cloud key — the operator's own model is added and answers", async () => {
    // Today's wizard asks for a cloud key; the own-model track skips it
    // (the stack has no network to a provider), and GitHub too.
    await expect(page.getByTestId("setup-steps-compact")).toContainText("Step 2 of 4 · Provider Key");
    await page.getByRole("button", { name: "Skip", exact: true }).click();
    await page.getByRole("button", { name: "Skip for now" }).click();
    await page.getByRole("button", { name: "Go to command center" }).click();
    await expect(page).not.toHaveURL(/\/setup/);

    const token = await operatorToken();
    if (!(await stubRuntime(token))) {
      const dialog = await probeAddress(page, STUB_URL);
      await expect(dialog.getByText(/Detected\s+OpenAI-compatible\s+·\s+1 model/)).toBeVisible();
      await expect(dialog.getByRole("combobox")).toHaveValue(STUB_MODEL);
      await dialog.getByRole("button", { name: "Add runtime" }).click();
      await expect(dialog).toHaveCount(0);
    }
    // Test on save is a separate click today: the runtime panel's probe.
    const panel = await reprobe(page, STUB_NAME);
    await expect(panel.getByText(/^Model (unchanged: stub-model|: .* → stub-model)$/)).toBeVisible();
    const rt = (await stubRuntime(token))!;
    expect(rt.model_identifier).toBe(STUB_MODEL);
    const check = await api<{ state: string; reason: string }>("GET", `/config-checks/runtime/${rt.id}`, token);
    expect(check).toMatchObject({ state: "ok", reason: "ok" });
  });

  const token = await operatorToken();
  const rt = (await stubRuntime(token))!;
  const board = await createBoard(token, `First run ${run}`);
  const title = `Add a test for parse() ${run}`;
  let agent!: FakeAgent;
  let taskId!: string;

  await test.step("S3–S4 first agent and first job (fixture until first-run, PR 3-1)", async () => {
    agent = await FakeAgent.create(token, board.id, "Lead", { lead: true, runtimeId: rt.id });
    const task = await createTask(token, board.id, title, agent.id);
    taskId = task.id;
    expect(await agent.takeTask()).toBe(task.id);
  });

  await test.step("S5 the agent works: Home shows the job as running", async () => {
    await page.evaluate(
      (b) => localStorage.setItem("mc-app-state", JSON.stringify({ state: { activeBoardId: b }, version: 0 })),
      board.id,
    );
    await page.goto("/");
    await expect(page.locator('[data-lane="in_progress"]').getByRole("button", { name: re(title) })).toBeVisible();
    await agent.comment("Wrote test_parse.py — 3 cases, all pass.", "progress");
  });

  await test.step("S9 the question card: the agent asks before it commits, one answer from the phone", async () => {
    const asked = await agent.question("Ready to commit: add test for parse() · 1 file · +14. Commit?", [
      "Commit",
      "Change something",
    ]);
    expect(asked.your_status).toBe("blocked");
    expect((await getTask(token, board.id, taskId)).status).toBe("blocked");

    await page.goto("/");
    const inboxTab = page.getByRole("navigation", { name: "Main navigation" }).getByRole("link", { name: /^Inbox\b/ });
    await expect(inboxTab).toHaveAccessibleName("Inbox, 1 waiting");
    await inboxTab.click();
    const card = page.getByTestId("approval-card").filter({ hasText: "Ready to commit" });
    await expect(card).toContainText(title);
    await card.getByRole("button", { name: "Commit", exact: true }).click();
    await card.getByRole("button", { name: "Reply" }).click();
    await expect(card).toHaveCount(0);
  });

  await test.step("S10 effect: the agent gets 'Commit', carries on and commits", async () => {
    const delivered = await agent.waitForDelivery((text) => /\bCommit\b/.test(text) && text.includes("Ready to commit"));
    expect(delivered).toMatch(/\*\*Antwort:\*\* Commit|Answer:\*\* Commit/);
    expect((await getTask(token, board.id, taskId)).status).toBe("in_progress");
    // A progress line is the evidence MC wants before review.
    await agent.comment("git commit -m \"test: add test for parse()\" — 1 file changed, 14 insertions(+)", "progress");
    await agent.setStatus("review");

    // End state: the question is gone; what waits now is the operator's
    // review of the commit (the later merge gate takes over from here).
    await page.goto("/");
    await expect(page.locator('[data-lane="review"]').getByRole("button", { name: re(title) })).toBeVisible();
    await expect(page.locator('[data-lane="blocked"]')).toHaveCount(0);
    const inboxTab = page.getByRole("navigation", { name: "Main navigation" }).getByRole("link", { name: /^Inbox\b/ });
    await expect(inboxTab).toHaveAccessibleName("Inbox, 1 waiting");
    await inboxTab.click();
    await expect(page.getByRole("main")).toContainText(title);
    await expect(page.getByTestId("approval-card")).toHaveCount(0);
  });
});

// Sabotage "model gone" (plan 3-5): the operator's model server is down.
// Both places that probe it must say so in words — not the client's raw
// error, and not "Model unchanged" over a dead endpoint (both found by this
// journey).
test("model gone: the add dialog and the runtime check name it, and it recovers", async ({ page }) => {
  const token = await operatorToken();
  await signIn(page, token);
  let rt = await stubRuntime(token);
  if (!rt) {
    // Run alone (--grep): bring the runtime the first test adds.
    await api("POST", "/runtimes/db", token, {
      slug: `stub-model-${run}`,
      display_name: STUB_NAME,
      runtime_type: "openai_compatible",
      endpoint: STUB_URL,
      model_identifier: STUB_MODEL,
      enabled: true,
    });
    rt = (await stubRuntime(token))!;
  }

  await withServiceDown("stub-model", async () => {
    const dialog = await probeAddress(page, STUB_URL);
    await expect(dialog.getByTestId("add-runtime-unreachable")).toHaveText(/^Endpoint unreachable\./);
    await expect(dialog.getByRole("button", { name: "Add runtime" })).toHaveCount(0);
    await dialog.getByRole("button", { name: "Close" }).click();

    const panel = await reprobe(page, STUB_NAME);
    await expect(panel.getByTestId("runtime-probe-reason")).toHaveText("No chat model answers at this endpoint.");
    await expect(panel.getByText(/Model unchanged/)).toHaveCount(0);
    const check = await api<{ state: string; reason: string }>("GET", `/config-checks/runtime/${rt!.id}`, token);
    expect(check).toMatchObject({ state: "fail", reason: "no_model" });
  });

  // Back up: the same click is green again.
  const panel = await reprobe(page, STUB_NAME);
  await expect(panel.getByText("Model unchanged: stub-model")).toBeVisible();
  await expect(panel.getByTestId("runtime-probe-reason")).toHaveCount(0);
});

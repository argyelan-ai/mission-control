// J-phone-needs-you (docs/produkt/landkarte.yaml): "When something waits for
// me, I want to see and answer it from my phone in a few taps."
// Steps F-home → F-inbox → F-tasks, at phone width. Each test ends on the
// effect at the agent (concept C6/F21), not on the click.
import { expect, test, type Page } from "@playwright/test";
import { FakeAgent } from "./lib/fakeHarness";
import { createBoard, createTask, getTask, operatorToken, signIn } from "./lib/mc";

test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

const run = Date.now().toString(36);

function lane(page: Page, key: string) {
  return page.locator(`[data-lane="${key}"]`);
}

test("a blocked task is unblocked from the phone and the agent gets the instruction", async ({ page }) => {
  const token = await operatorToken();
  const board = await createBoard(token, `Phone unblock ${run}`);
  const agent = await FakeAgent.create(token, board.id, "Fake Writer");
  const title = `Publish the release notes ${run}`;
  const task = await createTask(token, board.id, title, agent.id);
  await agent.takeTask();
  await agent.blockForDecision("Two changelogs disagree", "Which changelog is right: v1 or v2?");

  await signIn(page, token, board.id);

  // Home: the blocked task is visible as waiting for me.
  await page.goto("/");
  await expect(lane(page, "blocked").getByRole("button", { name: new RegExp(title) })).toBeVisible();

  // Tab bar → Inbox (phone menu B, #738): the tab already counts the one
  // waiting decision; the decision is there with the agent's question.
  const inboxTab = page.getByRole("navigation", { name: "Main navigation" }).getByRole("link", { name: /^Inbox\b/ });
  await expect(inboxTab).toHaveAccessibleName("Inbox, 1 waiting");
  await inboxTab.click();
  await expect(page).toHaveURL(/\/inbox/);
  const card = page.getByTestId("approval-card").filter({ hasText: title });
  await expect(card).toContainText("Which changelog is right: v1 or v2?");

  // Answer in a few taps.
  await card.getByRole("textbox", { name: "Instruction for the agent..." }).fill("Use v2, it has the security fix.");
  const before = agent.received.length;
  await card.getByRole("button", { name: "Unblock" }).click();
  await expect(card).toHaveCount(0);

  // Effect: the agent received the instruction and gets the task back
  // (only polls after the click count — takeTask() saw new_task already).
  const delivered = await agent.waitForDelivery((text) => text.includes("Use v2, it has the security fix."));
  expect(delivered).toContain("Use v2, it has the security fix.");
  const after = agent.received.slice(before);
  expect(after.some((p) => p.state === "new_task" && p.task?.id === task.id)).toBe(true);
  expect((await getTask(token, board.id, task.id)).status).not.toBe("blocked");

  // End state: nothing waits any more.
  await page.goto("/");
  // The card is back in the pipeline (loaded), just no longer blocked.
  await expect(page.getByRole("button", { name: new RegExp(title) })).toBeVisible();
  await expect(lane(page, "blocked")).toHaveCount(0);
});

// Found by this journey on 2026-09-29 as a known gap ("Reply" only posted a
// plain comment, which is not delivered while the task is `waiting`); closed
// by #712: the needs-you card shows the agent's open question with an answer
// field, and Reply sends a thread reply addressed to that question
// (reply_to), which clears it and resumes the task.
test("a waiting question is answered from the phone and the agent gets the answer", async ({ page }) => {
  const token = await operatorToken();
  const board = await createBoard(token, `Phone answer ${run}`);
  // Only a board lead can ask blocking questions today (workers have no
  // active-task lock) — the fake agent plays the lead.
  const agent = await FakeAgent.create(token, board.id, "Fake Lead", { lead: true });
  const title = `Choose the release name ${run}`;
  const task = await createTask(token, board.id, title, agent.id);
  await agent.takeTask();
  await agent.askBlocking("Which release name: Aurora or Borealis?");
  expect((await getTask(token, board.id, task.id)).status).toBe("waiting");

  await signIn(page, token, board.id);

  // Home: the waiting task is visible, one tap opens it.
  await page.goto("/");
  await lane(page, "waiting").getByRole("button", { name: new RegExp(title) }).click();
  const detail = page.getByRole("dialog", { name: "Task details" });
  await expect(detail.getByText(/^Waiting · Fake Lead asked/)).toBeVisible();

  // The card shows the agent's question with an answer field; Reply stays
  // off until there is an answer.
  const card = detail.getByTestId("task-state-card");
  await expect(card.getByTestId("state-card-question")).toHaveText("Which release name: Aurora or Borealis?");
  const reply = card.getByRole("button", { name: "Reply" });
  await expect(reply).toBeDisabled();
  await card.getByRole("textbox", { name: "Your answer" }).fill("Aurora.");
  await reply.click();

  // Effect: the agent receives the answer — once — and the task runs again.
  const delivered = await agent.waitForDelivery((text) => text.includes("Aurora."));
  expect(delivered).toContain("Aurora.");
  expect((await agent.deliveriesDuring(3_000)).filter((text) => text.includes("Aurora."))).toEqual([]);
  expect((await getTask(token, board.id, task.id)).status).toBe("in_progress");
  // The question is answered, so the card no longer asks it.
  await expect(card.getByTestId("state-card-question")).toHaveCount(0);
});

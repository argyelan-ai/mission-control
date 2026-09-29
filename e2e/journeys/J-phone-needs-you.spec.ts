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

  // Menu → Inbox: the decision is there with the agent's question.
  await page.getByRole("button", { name: "Open menu" }).click();
  await page.getByRole("link", { name: "Inbox", exact: true }).click();
  await expect(page).toHaveURL(/\/inbox/);
  const card = page.getByTestId("approval-card").filter({ hasText: title });
  await expect(card).toContainText("Which changelog is right: v1 or v2?");

  // Answer in a few taps.
  await card.getByRole("textbox", { name: "Instruction for the agent..." }).fill("Use v2, it has the security fix.");
  await card.getByRole("button", { name: "Unblock" }).click();
  await expect(card).toHaveCount(0);

  // Effect: the agent received the instruction and gets the task back.
  const delivered = await agent.waitForDelivery((text) => text.includes("Use v2, it has the security fix."));
  expect(delivered).toContain("Use v2, it has the security fix.");
  expect(agent.received.some((p) => p.state === "new_task" && p.task?.id === task.id)).toBe(true);
  expect((await getTask(token, board.id, task.id)).status).not.toBe("blocked");

  // End state: nothing waits any more.
  await page.goto("/");
  // The card is back in the pipeline (loaded), just no longer blocked.
  await expect(page.getByRole("button", { name: new RegExp(title) })).toBeVisible();
  await expect(lane(page, "blocked")).toHaveCount(0);
});

// KNOWN GAP (found by this journey, 2026-09-29): a blocking question
// (`mc ask --blocking`) parks the task in `waiting`. The task's "Reply"
// button only posts a plain comment, and comments are not delivered to the
// agent while the task is `waiting` (agents.py _collect_and_ack_new_comments
// has no `waiting` in its status list) — so the agent never gets the answer
// and the task stays `waiting`. The answer path that does resume the task is
// POST /tasks/{id}/thread/messages with reply_to, which no UI calls yet.
// This test pins the gap: every step up to the answer must work, and the
// last assertion states that the answer does NOT arrive. When the gap is
// fixed this test turns red — then flip the last block into the real
// effect check (answer delivered, task back in_progress).
test("a waiting question is answered from the phone [known gap: answer does not reach the agent]", async ({ page }) => {
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

  // Reply.
  await detail.getByRole("button", { name: "Reply" }).click();
  const box = detail.getByRole("textbox", { name: "Add comment" });
  await box.fill("Aurora.");
  await box.press("Enter");
  await expect(detail.getByText("Aurora.", { exact: true })).toBeVisible();

  // The gap, pinned: the answer does not reach the agent and the task stays
  // parked. Turns red once fixed (see the comment above the test).
  const delivered = await agent.deliveriesDuring(6_000);
  expect(delivered.filter((text) => text.includes("Aurora."))).toEqual([]);
  expect((await getTask(token, board.id, task.id)).status).toBe("waiting");
});

// J-usage (docs/produkt/landkarte.yaml): "When I want to know what the work
// cost and how much ran locally, I want tokens and cost per week, source and
// task." Step F-insights, at phone width.
//
// Usage rows normally come from the token harvester reading real transcripts,
// so there is no API to create them: the fixture writes known rows straight
// into the test database and the journey checks that Insights shows exactly
// those numbers. Heads rows carry their own locality, so the local share
// does not depend on the runtime catalogue.
import { expect, test } from "@playwright/test";
import { operatorToken, signIn, sql } from "./lib/mc";

test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, timezoneId: "UTC" });

type Row = { harness: string; locality: "local" | "cloud"; input: number; output: number; cost: number; age: string };

const ROWS: Row[] = [
  // this week: 600K local (400K in + 200K out), 200K cloud (150K + 50K) for $4.75
  { harness: "head-omp", locality: "local", input: 400_000, output: 200_000, cost: 0, age: "1 minute" },
  { harness: "head-claude", locality: "cloud", input: 150_000, output: 50_000, cost: 4.75, age: "1 minute" },
  // same time last week: 100K local (50K out), 100K cloud (20K out) for $1.00
  { harness: "head-omp", locality: "local", input: 50_000, output: 50_000, cost: 0, age: "7 days 1 minute" },
  { harness: "head-claude", locality: "cloud", input: 80_000, output: 20_000, cost: 1.0, age: "7 days 1 minute" },
];

function isoWeek(d: Date): number {
  const t = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  t.setUTCDate(t.getUTCDate() + 4 - (t.getUTCDay() || 7));
  const yearStart = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
  return Math.ceil(((t.getTime() - yearStart.getTime()) / 86_400_000 + 1) / 7);
}

test("cost and local share per week, day and source match the recorded usage", async ({ page }) => {
  const now = new Date();
  test.skip(now.getUTCHours() === 0 && now.getUTCMinutes() < 5, "rows 1 minute old would fall on yesterday");

  const token = await operatorToken();
  // Fresh stack per night; clear anyway so a re-run on the same stack is exact.
  sql("delete from model_usage_events");
  const values = ROWS.map(
    (r, i) =>
      `(gen_random_uuid(), '${r.harness}', 'journey-run-${i}', '${r.locality}', '${r.locality}-model', ` +
      `'journey-session', 'journey-usage-${i}', ${r.input}, ${r.output}, ${r.cost}, now() - interval '${r.age}')`,
  );
  sql(
    "insert into model_usage_events (id, harness, head_run_id, locality, model, session_id, message_uuid, " +
      `input_tokens, output_tokens, cost_usd, ts) values ${values.join(", ")}`,
  );

  await signIn(page, token);
  await page.goto("/insights");
  const main = page.getByRole("main");

  // Hero: this week's cost and local share (output tokens: 200K of 250K).
  await expect(main.getByTestId("hero-cost")).toHaveText("$4.75");
  const local = main.getByTestId("hero-local");
  await expect(local).toContainText(/80%\s*ran locally/);
  await expect(local).toContainText("last week 71.4%"); // 50K of 70K output tokens

  // Today in the day grid: 800K tokens, the cost and 75% local (all tokens).
  await expect(main.getByTestId("heatmap-day")).toContainText(/800K tokens · \$4\.75 · 75% local/);

  // Where it came from: only the cloud heads cost money.
  const sources = main.getByRole("region", { name: "Where usage comes from" });
  await expect(sources.getByTestId("source-heads:cloud")).toContainText(/Heads cloud.*\$4\.75/);

  // Local share per week: last week and this week.
  const trend = main.getByRole("list", { name: /Local share of output tokens/ });
  const lastWeek = new Date(now.getTime() - 7 * 86_400_000);
  await expect(trend.getByRole("listitem", { name: `W${isoWeek(lastWeek)}: 71.4%` })).toBeVisible();
  await expect(trend.getByRole("listitem", { name: `W${isoWeek(now)}: 80%` })).toBeVisible();
});

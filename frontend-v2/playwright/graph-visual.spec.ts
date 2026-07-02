import { test, expect } from "@playwright/test";

test("graph view renders with community colors after cleanup", async ({ page }) => {
  // JWT is provided via env var MC_JWT — orchestrator-friendly
  const token = process.env.MC_JWT ?? "";
  await page.goto("http://localhost/");
  if (token) {
    await page.evaluate((t) => localStorage.setItem("mc_auth_token", t), token);
  }
  await page.goto("http://localhost/memory?view=graph", { waitUntil: "networkidle" });
  await page.waitForTimeout(4000);  // let force-simulation settle

  // The header chip should reflect a sensible post-cleanup state (< 500 nodes)
  const statsText = await page.locator("text=NOTES").first().textContent().catch(() => null);
  if (statsText) {
    const match = statsText.match(/(\d+)\s+NOTES/i);
    if (match) {
      expect(Number(match[1])).toBeLessThan(500);
    }
  }

  // Canvas must be visible (basic smoke)
  const canvas = page.locator("canvas").first();
  await expect(canvas).toBeVisible({ timeout: 10000 });

  // Color-mode select default = "community"
  const colorSelect = page.locator('select').filter({ hasText: /community|type/i }).first();
  if (await colorSelect.count() > 0) {
    const value = await colorSelect.inputValue();
    expect(value).toBe("community");
  }
});

#!/usr/bin/env node
// Composition budget ("Messblatt") for one region of one page — DESIGN.md
// "Komposition" K3–K9. Read-only like the probe: same write lock, same token
// handling. Writes budget.json, a phone/desktop picture and a 10 px blur
// picture (one loud thing per region should still stand out, K1).
//
// Report-only by default: the numbers are a floor, not a goal (K14). --strict
// exits 1 on findings, for when the limits have been calibrated on a page.
//
//   npm run design:budget -- --url http://localhost:3001/tasks?task=<id> --region "[data-region=task-head]"
import { mkdirSync, writeFileSync } from "node:fs";
import { execSync } from "node:child_process";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { chromium } from "playwright";
import { createLockedContext, newWriteCounter } from "./lib/context.mjs";
import { collectRegion, compositionBudget, measuredBuild, parseBudgetArgs } from "./lib/composition.mjs";

const HELP = `Usage: npm run design:budget -- --url URL --region CSS [--width 390,1440] [--out DIR] [--light] [--wait CSS] [--first-screen] [--strict]
  Token (only for the MC UI): MC_PROBE_TOKEN, never printed. Measure a branch on its own dev server (:3001).`;

let opts;
try {
  opts = parseBudgetArgs(process.argv.slice(2));
} catch (e) {
  console.error(String(e.message || e));
  console.error(HELP);
  process.exit(64);
}
if (opts.help) {
  console.log(HELP);
  process.exit(0);
}

const build = measuredBuild(opts.url);
if (build.warning) console.warn(`warning: ${build.warning}`);
let head = "unknown";
try {
  head = execSync("git rev-parse --short HEAD", { stdio: ["ignore", "pipe", "ignore"] }).toString().trim();
} catch {}

const OUT = resolve(opts.out || join(tmpdir(), "mc-design-budget", new Date().toISOString().replace(/[:.]/g, "-")));
mkdirSync(OUT, { recursive: true });
const TOKEN = process.env.MC_PROBE_TOKEN || "";
const origin = opts.url.startsWith("file:") ? null : new URL(opts.url).origin;

const writes = newWriteCounter();
const browser = await chromium.launch();
const results = [];
try {
  for (const width of opts.widths) {
    const ctx = await createLockedContext(browser, width, TOKEN && origin ? TOKEN : null, writes, origin || undefined);
    if (opts.light) await ctx.addInitScript(() => document.documentElement.setAttribute("data-theme", "light"));
    const page = await ctx.newPage();
    await page.emulateMedia({ colorScheme: opts.light ? "light" : "dark" });
    await page.goto(opts.url, { waitUntil: "networkidle", timeout: 30000 }).catch(() => {});
    if (opts.wait) await page.waitForSelector(opts.wait, { timeout: 20000 }).catch(() => {});
    await page.waitForTimeout(800);
    const raw = await page.evaluate(collectRegion, { selector: opts.region, firstScreen: opts.firstScreen });
    const tag = `${width}-${opts.light ? "light" : "dark"}`;
    if (raw.error) {
      results.push({ width, error: raw.error });
      console.error(`${width}px: ${raw.error}`);
      await ctx.close();
      continue;
    }
    const budget = compositionBudget(raw.nodes, { boxDepth: raw.boxDepth, items: raw.items });
    await page.screenshot({ path: join(OUT, `${tag}.png`) });
    await page.addStyleTag({ content: "html{filter:blur(10px)}" });
    await page.screenshot({ path: join(OUT, `${tag}-blur.png`) });
    results.push({ width, theme: opts.light ? "light" : "dark", regionHeight: raw.height, ...budget });
    const line = budget.findings.map((f) => `${f.rule} ${f.what} ${f.value}>${f.limit}`).join(" · ") || "no findings";
    console.log(`${tag}: ${line}`);
    await ctx.close();
  }
} finally {
  await browser.close();
}

if (writes.passed > 0) {
  console.error("A write request got through — stop and report this.");
  process.exit(2);
}
const report = { url: opts.url.replace(/\?.*$/, "?…"), region: opts.region, firstScreen: opts.firstScreen, build: build.kind, head, measuredAt: new Date().toISOString(), results };
writeFileSync(join(OUT, "budget.json"), JSON.stringify(report, null, 2));
console.log(`budget: ${join(OUT, "budget.json")}`);
const failed = results.some((r) => r.error || r.passed === false);
process.exit(opts.strict && failed ? 1 : 0);

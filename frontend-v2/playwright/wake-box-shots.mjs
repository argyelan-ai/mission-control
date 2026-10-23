/**
 * Picture gate for "wake a sleeping box from its host flag" (round-5 re-check L1).
 *
 * Builds the REAL `FleetStage` from source with the project's compiled
 * Tailwind (fixture: playwright/wake-box-entry.tsx — every API call stubbed,
 * no box is woken) and saves, at 390 px (iPhone 13) and 1280 px, in EN and DE:
 *   1. the sleeping box: "asleep" + "Wake", no "Start model"
 *   2. right after "Wake": "Waking…" + how long it takes
 *   3. its cockpit: "asleep" instead of telemetry, "Wake" instead of Restart
 *   4. Wake while the Mac's wake helper is not running (503): "not woken",
 *      no "Waking…" (PR #976 review, MUST 1)
 *   5. the runtime detail's Wake with the helper down: the same sentence,
 *      not a bare "Wake failed." (round-7 L5, r1 N1)
 *   6. the runtime detail's Wake sent: the translated "1–2 min" line, not
 *      the backend's raw German message
 *
 * Checks what jsdom cannot: no sideways scroll, Wake tap target >= 44 px (K11).
 *
 * Aufruf (aus frontend-v2/):  node playwright/wake-box-shots.mjs [outDir]
 */
import { chromium, devices } from "playwright";
import { build } from "vite";
import react from "@vitejs/plugin-react";
import postcss from "postcss";
import tailwind from "@tailwindcss/postcss";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const DIST = fs.mkdtempSync(path.join(os.tmpdir(), "wake-box-dist-"));
const OUT = path.resolve(process.argv[2] ?? fs.mkdtempSync(path.join(os.tmpdir(), "wake-box-shots-")));
fs.mkdirSync(OUT, { recursive: true });

async function buildFixture() {
  const cssPath = path.join(ROOT, "src/styles/globals.css");
  const compiled = await postcss([tailwind()]).process(fs.readFileSync(cssPath, "utf-8"), {
    from: cssPath,
    to: path.join(DIST, "globals.css"),
  });
  await build({
    root: ROOT,
    configFile: false,
    logLevel: "error",
    plugins: [react()],
    resolve: { alias: { "@": path.join(ROOT, "src") } },
    define: { "process.env.NODE_ENV": '"production"' },
    build: {
      outDir: DIST,
      emptyOutDir: true,
      minify: false,
      lib: { entry: path.join(HERE, "wake-box-entry.tsx"), formats: ["es"], fileName: "app" },
      rollupOptions: { output: { inlineDynamicImports: true } },
    },
  });
  const appFile = fs.readdirSync(DIST).find((f) => /^app\.(m?js)$/.test(f));
  if (!appFile) throw new Error("no bundle produced in " + DIST);
  fs.writeFileSync(path.join(DIST, "index.html"), `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<style>${compiled.css}</style></head>
<body class="font-sans antialiased bg-[var(--color-bg-deep)] text-[var(--color-text-primary)] min-h-[100dvh] overflow-x-hidden">
<div id="root"></div>
<script>window.process = { env: { NODE_ENV: "production" }, platform: "linux", version: "" };</script>
<script type="module">import { mount } from "./${appFile}"; mount();</script>
</body></html>`);
}

const MIME = { ".html": "text/html", ".mjs": "text/javascript", ".js": "text/javascript", ".css": "text/css" };

function serve() {
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      const url = new URL(req.url, "http://x");
      let p = path.join(DIST, decodeURIComponent(url.pathname));
      if (url.pathname === "/" || !fs.existsSync(p) || fs.statSync(p).isDirectory()) p = path.join(DIST, "index.html");
      res.writeHead(200, { "content-type": MIME[path.extname(p)] ?? "application/octet-stream" });
      res.end(fs.readFileSync(p));
    });
    server.listen(0, "127.0.0.1", () => resolve(server));
  });
}

const VIEWPORTS = [
  { name: "390", ctx: { ...devices["iPhone 13"] } },
  { name: "1280", ctx: { viewport: { width: 1280, height: 900 } } },
];
const LOCALES = ["en", "de"];

await buildFixture();
const server = await serve();
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch();
const failures = [];
let shots = 0;

for (const vp of VIEWPORTS) {
  const ctx = await browser.newContext({ ...vp.ctx, colorScheme: "dark" });
  for (const locale of LOCALES) {
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e).slice(0, 200)));
    await page.goto(origin, { waitUntil: "load" });
    await page.evaluate((locale) => {
      document.documentElement.lang = locale;
      document.documentElement.setAttribute("data-theme", "dark");
    }, locale);
    await page.waitForFunction(() => typeof window.__mountWakeBox === "function", { timeout: 30000 });
    await page.evaluate((o) => window.__mountWakeBox(o), { locale });
    const card = page.locator('[data-testid="asleep-box"][data-host-slug="ws"]');
    const wake = card.getByTestId("wake-box");
    await wake.waitFor({ timeout: 30000 });
    await page.waitForTimeout(300);
    const label = `${vp.name} · ${locale}`;

    const m = await page.evaluate(() => ({
      hScroll: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      wakeH: document.querySelector('[data-testid="asleep-box"] [data-testid="wake-box"]').getBoundingClientRect().height,
      startInCard: document.querySelector('[data-testid="asleep-box"]').textContent.includes("Start model") ||
        document.querySelector('[data-testid="asleep-box"]').textContent.includes("Modell starten"),
    }));
    if (m.hScroll > 0) failures.push(`${label}: page scrolls sideways by ${m.hScroll}px`);
    if (m.wakeH < 44) failures.push(`${label}: Wake tap target ${m.wakeH}px < 44px (K11)`);
    if (m.startInCard) failures.push(`${label}: sleeping card still offers Start model`);

    const shoot = async (name) => {
      const file = path.join(OUT, `${vp.name}-${locale}-${name}.png`);
      await page.screenshot({ path: file, fullPage: true });
      shots += 1;
      console.log(`${label}: ${name} -> ${file}`);
    };
    await shoot("1-asleep");
    await wake.click();
    await card.getByTestId("wake-sent").waitFor({ timeout: 5000 });
    await page.waitForTimeout(150);
    await shoot("2-wake-sent");
    await card.locator(".stage-actions-gear").click();
    await page.getByTestId("cockpit-asleep").waitFor({ timeout: 5000 });
    await page.waitForTimeout(400);
    if (await page.getByTestId("cockpit-restart").count()) failures.push(`${label}: cockpit still offers Restart`);
    if (!(await page.getByTestId("box-cockpit").getByTestId("wake-box").isDisabled())) {
      failures.push(`${label}: cockpit offers Wake again after the card sent it`);
    }
    await page.getByTestId("box-cockpit").getByTestId("wake-box").scrollIntoViewIfNeeded();
    const file = path.join(OUT, `${vp.name}-${locale}-3-cockpit.png`);
    await page.screenshot({ path: file });
    shots += 1;
    console.log(`${label}: 3-cockpit -> ${file}`);
    // 4. fresh mount, the wake helper on the Mac is not running.
    await page.evaluate((locale) => {
      window.__wakeHelperDown = true;
      window.__mountWakeBox({ locale, helperDown: true });
    }, locale);
    const card4 = page.locator('[data-testid="asleep-box"][data-host-slug="ws"]');
    await card4.getByTestId("wake-box").waitFor({ timeout: 30000 });
    await page.waitForTimeout(300);
    await card4.getByTestId("wake-box").click();
    await card4.getByTestId("wake-failed").waitFor({ timeout: 5000 });
    if (await card4.getByTestId("wake-sent").count()) failures.push(`${label}: helper down but card says "Waking…"`);
    const h4 = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    if (h4 > 0) failures.push(`${label}: helper-down card scrolls sideways by ${h4}px`);
    await page.waitForTimeout(150);
    await shoot("4-helper-down");

    // 5./6. the second wake path: the runtime detail panel.
    for (const [n, down, want] of [
      ["5-detail-helper-down", true, locale === "de" ? "Weck-Helfer auf dem Mac läuft nicht" : "wake helper on the Mac is not running"],
      ["6-detail-sent", false, locale === "de" ? "Die Box braucht 1–2 Min." : "The box needs 1–2 min."],
    ]) {
      await page.evaluate(([locale, down]) => {
        window.__wakeHelperDown = down;
        window.__mountRuntimeDetail({ locale, helperDown: down });
      }, [locale, down]);
      const wakeBtn = page.getByRole("dialog").getByTitle(locale === "de" ? "Aufwecken" : "Wake");
      await wakeBtn.waitFor({ timeout: 30000 });
      await page.waitForTimeout(400);
      await wakeBtn.click();
      await page.getByText(want).first().waitFor({ timeout: 5000 });
      if (await page.getByText(/ausgelöst|Wake failed|Aufwecken fehlgeschlagen/).count()) {
        failures.push(`${label}: ${n} shows a raw or bare wake message`);
      }
      const h = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      if (h > 0) failures.push(`${label}: ${n} scrolls sideways by ${h}px`);
      await page.waitForTimeout(200);
      const f = path.join(OUT, `${vp.name}-${locale}-${n}.png`);
      await page.screenshot({ path: f });
      shots += 1;
      console.log(`${label}: ${n} -> ${f}`);
    }
    if (errors.length) failures.push(`${label}: page errors: ${errors.join(" | ")}`);
    await page.close();
  }
  await ctx.close();
}

await browser.close();
server.close();
fs.rmSync(DIST, { recursive: true, force: true });

if (failures.length) {
  console.error("\nFAIL:\n" + failures.map((f) => "  - " + f).join("\n"));
  process.exit(1);
}
console.log(`\nOK: ${shots} pictures in ${OUT}.`);

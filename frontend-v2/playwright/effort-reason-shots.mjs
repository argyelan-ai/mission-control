/**
 * Picture gate for the locked effort chip's reason popover (PR #932).
 *
 * ADR-090 §1 / §5.3: new visible UI is accepted on a 390 px phone, in both
 * languages and both themes. This builds the REAL `Composer` from source with
 * the project's real compiled Tailwind, taps the locked chip in real Chromium
 * (iPhone 13 profile) and saves one picture per language × theme × reason.
 *
 * It also checks the one layout question the picture gate has to answer: the
 * right-anchored popover (w-64) must lie fully inside the viewport. jsdom has
 * no layout, so this cannot live in vitest.
 *
 * Aufruf (aus frontend-v2/):  node playwright/effort-reason-shots.mjs [outDir]
 * Exit code 0 = every popover fits; 1 = one leaves the screen (named).
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
const DIST = fs.mkdtempSync(path.join(os.tmpdir(), "effort-reason-dist-"));
const OUT = path.resolve(process.argv[2] ?? fs.mkdtempSync(path.join(os.tmpdir(), "effort-reason-shots-")));
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
      lib: { entry: path.join(HERE, "effort-reason-entry.tsx"), formats: ["es"], fileName: "app" },
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

const REASONS = [
  { reason: "harness_no_effort", harness: "hermes" },
  { reason: "acp_state_missing", harness: null },
];
const LOCALES = ["en", "de"];
const THEMES = ["dark", "light"];

await buildFixture();
const server = await serve();
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch();
const failures = [];

for (const theme of THEMES) {
  const ctx = await browser.newContext({ ...devices["iPhone 13"], colorScheme: theme });
  for (const locale of LOCALES) {
    for (const { reason, harness } of REASONS) {
      const page = await ctx.newPage();
      const errors = [];
      page.on("pageerror", (e) => errors.push(String(e).slice(0, 200)));
      await page.goto(origin, { waitUntil: "load" });
      await page.evaluate(({ theme, locale }) => {
        const html = document.documentElement;
        html.lang = locale;
        // ADR-087: :root is dark; [data-theme="light"] switches the tokens.
        if (theme === "light") html.setAttribute("data-theme", "light");
        else html.setAttribute("data-theme", "dark");
        html.style.colorScheme = theme;
      }, { theme, locale });
      await page.waitForFunction(() => typeof window.__mountEffortReason === "function", { timeout: 30000 });
      await page.evaluate((o) => window.__mountEffortReason(o), { locale, reason, harness });
      const chip = page.getByTestId("effort-chip-static");
      await chip.waitFor({ timeout: 30000 });
      await chip.tap();
      const pop = page.getByTestId("effort-reason");
      await pop.waitFor({ timeout: 5000 });
      await page.waitForTimeout(200);

      const label = `${reason} · ${locale} · ${theme}`;
      const m = await page.evaluate(() => {
        const r = document.querySelector('[data-testid="effort-reason"]').getBoundingClientRect();
        const c = document.querySelector('[data-testid="effort-chip-static"]').getBoundingClientRect();
        return {
          pop: { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height },
          chip: { width: c.width, height: c.height },
          vw: document.documentElement.clientWidth,
          vh: window.innerHeight,
          hScroll: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        };
      });
      const text = (await pop.innerText()).trim();
      const file = path.join(OUT, `effort-reason-${reason}-${locale}-${theme}.png`);
      await page.screenshot({ path: file });
      console.log(`${label}: popover ${Math.round(m.pop.left)}..${Math.round(m.pop.right)} of ${m.vw}px, ` +
        `top ${Math.round(m.pop.top)}, ${Math.round(m.pop.height)}px high; chip ${m.chip.width}x${m.chip.height}; ` +
        `"${text}" -> ${file}`);

      if (m.pop.left < 0 || m.pop.right > m.vw || m.pop.top < 0 || m.pop.bottom > m.vh) {
        failures.push(`${label}: popover leaves the screen ${JSON.stringify(m.pop)} (viewport ${m.vw}x${m.vh})`);
      }
      if (m.hScroll > 0) failures.push(`${label}: page scrolls sideways by ${m.hScroll}px`);
      if (m.chip.height < 44) failures.push(`${label}: chip tap target ${m.chip.height}px < 44px (K11)`);
      if (!text) failures.push(`${label}: popover is empty`);
      if (errors.length) failures.push(`${label}: page errors: ${errors.join(" | ")}`);
      await page.close();
    }
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
console.log(`\nOK: ${THEMES.length * LOCALES.length * REASONS.length} pictures in ${OUT}, every popover inside the screen.`);

/**
 * Live browser panel on a phone — real-layout check + state screenshots.
 *
 * Why not vitest: "the toolbar stays on one line at 393 px" is a layout fact
 * and jsdom computes no layout. This builds the REAL `BrowserLiveView` from
 * source (real Tailwind, real i18n catalog), serves it on a throwaway port
 * and drives it in real Chromium — no backend, no login (same approach as
 * chat-transcript-width.mjs).
 *
 * Checks, per phone width (393, 430) and theme (dark, light):
 *   - the toolbar is ONE line: the fullscreen button sits next to the page
 *     picker (it used to drop to a second line at 393 px), nothing else wraps
 *     either, and the page does not scroll sideways;
 *   - the panel states render: own tab (scoped), the unassigned fallback
 *     (agent owns no tab, unassigned tabs exist → every tab + hint, never
 *     "no open tab"), the real "no open tab" (every tab is another agent's),
 *     and "all tabs".
 *
 * Aufruf (aus frontend-v2/):
 *   node playwright/browser-live-toolbar.mjs [--out <dir>] [--name <agent name>]
 * Exit 0 = layout holds; 1 = a toolbar wrapped or the page overflowed.
 */
import { chromium } from "playwright";
import { build } from "vite";
import react from "@vitejs/plugin-react";
import postcss from "postcss";
import tailwind from "@tailwindcss/postcss";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const DIST = path.join(HERE, ".browser-live-dist");

const arg = (name, fallback) => {
  const i = process.argv.indexOf(name);
  return i > 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
};
const OUT = arg("--out", null);
const AGENT = arg("--name", "Alpha");

const OWN = { id: "own", title: "Example Domain", url: "https://example.org/" };
const FREE = { id: "free", title: "Example Domain", url: "https://example.org/", unassigned: true };
const OTHER = { id: "other", title: "Checkout", url: "https://shop.example/checkout" };

/** State → REST answer + WS messages, in the shapes the backend sends. */
const STATES = {
  "scoped-own-tab": {
    rest: { targets: [OWN], scopeUnavailable: false, unassignedFallback: false, unassignedCount: 0 },
    ws: [
      { type: "status", code: "scope_unavailable", active: false },
      { type: "status", code: "unassigned_fallback", active: false, count: 0 },
      { type: "attached", target: OWN },
      { type: "targets", targets: [OWN], activeId: "own", followedId: "own" },
    ],
    frame: OWN,
  },
  "unassigned-fallback": {
    rest: { targets: [FREE, OTHER], scopeUnavailable: false, unassignedFallback: true, unassignedCount: 1 },
    ws: [
      { type: "status", code: "scope_unavailable", active: false },
      { type: "status", code: "unassigned_fallback", active: true, count: 1 },
      { type: "attached", target: FREE },
      { type: "targets", targets: [FREE, OTHER], activeId: "free", followedId: "free" },
    ],
    frame: FREE,
  },
  "scoped-empty": {
    rest: { targets: [], scopeUnavailable: false, unassignedFallback: false, unassignedCount: 0 },
    ws: [
      { type: "status", code: "scope_unavailable", active: false },
      { type: "status", code: "unassigned_fallback", active: false, count: 0 },
      { type: "targets", targets: [], activeId: null, followedId: null },
      { type: "status", code: "no_page" },
    ],
    frame: null,
  },
  "all-tabs": {
    showAllTabs: true,
    rest: { targets: [OTHER, OWN], scopeUnavailable: false },
    ws: [
      { type: "attached", target: OTHER },
      { type: "targets", targets: [OTHER, OWN], activeId: "other", followedId: "other" },
    ],
    frame: OTHER,
  },
};

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
      lib: { entry: path.join(HERE, "browser-live-entry.tsx"), formats: ["es"], fileName: "app" },
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

await buildFixture();
const server = await serve();
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch();
const failures = [];
const rows = [];
if (OUT) fs.mkdirSync(OUT, { recursive: true });

for (const width of [393, 430]) {
  for (const theme of ["dark", "light"]) {
    for (const [name, state] of Object.entries(STATES)) {
      const ctx = await browser.newContext({
        viewport: { width, height: 760 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true,
        colorScheme: theme,
      });
      await ctx.route("**/api/v1/auth/stream-ticket", (r) =>
        r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ticket: "t" }) }));
      await ctx.route("**/api/v1/browser-live/targets*", (r) =>
        r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(state.rest) }));
      const page = await ctx.newPage();
      const errors = [];
      page.on("pageerror", (e) => errors.push(String(e).slice(0, 200)));
      await page.goto(origin, { waitUntil: "load" });
      await page.evaluate((t) => {
        if (t === "light") document.documentElement.setAttribute("data-theme", "light");
        document.documentElement.style.colorScheme = t;
      }, theme);
      await page.waitForFunction(() => typeof window.__blvMount === "function", { timeout: 30000 });
      await page.evaluate((o) => window.__blvMount(o), { agentName: AGENT, showAllTabs: !!state.showAllTabs });
      await page.waitForTimeout(300);
      for (const msg of state.ws) await page.evaluate((m) => window.__blvPush(m), msg);
      if (state.frame) {
        await page.evaluate(({ title, url }) =>
          window.__blvPush({ type: "frame", data: window.__blvFrame(title, url), metadata: {} }), state.frame);
      }
      await page.waitForTimeout(300);

      const m = await page.evaluate(() => window.__blvMeasure());
      const label = `${name} @ ${width} ${theme}`;
      let oneLine = null;
      if (m.picker && m.fullscreen) {
        oneLine = Math.abs(m.picker.top - m.fullscreen.top) < 2;
        if (!oneLine) {
          failures.push(`${label}: fullscreen button wrapped (picker top ${m.picker.top}, fullscreen top ${m.fullscreen.top})`);
        }
      }
      // One 44 px row + py-2 + the bottom border = 61 px. Anything taller
      // means SOMETHING in the toolbar (e.g. the live/connecting status)
      // went onto a second line, even if the picker and fullscreen didn't.
      if (m.toolbar && m.toolbar.height > 62) {
        failures.push(`${label}: toolbar is ${Math.round(m.toolbar.height)}px tall — more than one line`);
      }
      if (m.docOverflow > 0) failures.push(`${label}: page scrolls sideways by ${m.docOverflow}px`);
      if (errors.length) failures.push(`${label}: page errors: ${errors.join(" | ")}`);
      rows.push({ label, oneLine, toolbarH: m.toolbar ? Math.round(m.toolbar.height) : null });
      if (OUT) await page.screenshot({ path: path.join(OUT, `${name}-${width}-${theme}.png`) });
      await ctx.close();
    }
  }
}

await browser.close();
server.close();
fs.rmSync(DIST, { recursive: true, force: true });

console.log("\n  browser panel on a phone");
for (const r of rows) {
  const bad = failures.some((f) => f.startsWith(r.label));
  console.log(`  ${bad ? "FAIL" : "ok  "}  ${r.label}  toolbar-one-line=${r.oneLine ?? "n/a"} toolbar-h=${r.toolbarH ?? "n/a"}px`);
}
if (failures.length) {
  console.log("\n" + failures.map((f) => "  - " + f).join("\n"));
  process.exit(1);
}

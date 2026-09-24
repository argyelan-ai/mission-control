/**
 * Width contract of the Sessions chat transcript — real-layout guard.
 *
 * Why this is not a vitest test: the contract is "the transcript's scroll box
 * has no horizontal scroll range". jsdom computes no layout, so `scrollWidth`
 * there is a stub and any such assertion would be vacuous. This measures real
 * layout in real Chromium against the project's real compiled Tailwind.
 *
 * Why no live app: `group-walkthrough.mjs` needs `MC_JWT` and a running
 * backend. This builds the real `ChatView` from source and serves it on a
 * throwaway port, so it runs anywhere `npm ci` has run.
 *
 * Aufruf (aus frontend-v2/):  node playwright/chat-transcript-width.mjs
 *
 * Exit code 0 = contract holds; 1 = a leak, with the offending boxes named.
 * Operator 18.09.2026: "scrollen geht manchmal nicht und manchmal kann man
 * horizontal scrollen" — the sideways part is this contract.
 */
import { chromium, devices } from "playwright";
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
const DIST = path.join(HERE, ".chat-width-dist");

/** 122 chars, no space, no punctuation: it cannot break without a wrap rule. */
const UNBREAKABLE = "x".repeat(122);
/** A path breaks at "/" on its own — normal input that must never leak. */
const LONG_PATH =
  "/home/agent/.omp/tasks/c53c85f3-ef97-41a0-9fac-cfaac5712ceb/notes/sehr-langer-dateiname-ohne-leerzeichen.md";

const now = Date.now();
const iso = (msAgo) => new Date(now - msAgo).toISOString();

/**
 * The two known leaks plus their realistic shapes, and the two element kinds
 * that are ALLOWED to exceed the width inside their own scroller.
 */
function fixture() {
  const events = [
    // Leak 1: user bubble, PLAIN text, one unbreakable token. No code span, so
    // MarkdownContent's own `overflow-wrap: anywhere` does not apply.
    { kind: "message", uuid: "w1", ts: iso(900000), role: "user", sidechain: false, model: null, text: UNBREAKABLE },
    // Leak 2: command with NO result — the plain one-liner branch, the only
    // place a command renders without `truncate`.
    { kind: "command", uuid: "w2", ts: iso(890000), command: `/${UNBREAKABLE}` },
    // The same two defects in realistic shapes.
    { kind: "message", uuid: "w3", ts: iso(880000), role: "user", sidechain: false, model: null, text: `Bitte ${UNBREAKABLE} prüfen` },
    { kind: "command", uuid: "w4", ts: iso(870000), command: `/context ${LONG_PATH}` },
    // Every other row type that carries raw text, each with the same
    // unbreakable token. These had class-name-only unit tests; the observable
    // contract (no horizontal scroll range) is what actually has to hold.
    { kind: "thinking", uuid: "w6", ts: iso(868000), sidechain: false, text: `Ich schaue in ${LONG_PATH} nach, weil ${UNBREAKABLE}` },
    // Assistant body: plain paragraph, no code span (an inline `<code>` carries
    // its own `overflow-wrap: anywhere` and would mask a missing wrap rule).
    { kind: "message", uuid: "w10", ts: iso(867000), role: "assistant", sidechain: false, model: "claude-opus-4",
      text: `Kurz gesagt: ${UNBREAKABLE}` },
    { kind: "tool", uuid: "w7", ts: iso(866000), toolUseId: "tu7", name: "Read", sidechain: false, status: "ok",
      title: `Read ${UNBREAKABLE}`, detail: { file_path: LONG_PATH }, result: UNBREAKABLE },
    { kind: "message", uuid: "w8", ts: iso(864000), role: "teammate", sidechain: false, model: null, teammate: "rex", text: `Recherche fertig.\n${UNBREAKABLE}` },
    { kind: "notification", uuid: "w9", ts: iso(862000), taskId: LONG_PATH, toolUseId: null, status: "completed", summary: `Fertig: ${LONG_PATH}` },
    // ── REAL operator transcript (card 630bdd4b, iPhone screenshots) ──
    // What the operator actually had on screen after #634 — none of it is an
    // unbreakable token, it is text WITH spaces. A fenced block WITHOUT a
    // language tag renders through MarkdownContent's INLINE code branch (no
    // `language-` className), so it has no overflow-x-auto of its own and the
    // surrounding <pre> keeps `white-space: pre` — spaces do not wrap there.
    { kind: "message", uuid: "r1", ts: iso(858000), role: "assistant", sidechain: false, model: "glm-5.3",
      text: [
        "Build läuft:",
        "",
        "```",
        "docker compose --profile n-control build --no-cache frontend",
        "ss | Rex | letzte echte Aeusserung vor 1 min | Rueckmeldung an Boss fehlt noch",
        "```",
      ].join("\n") },
    // Inline code carrying a shell command WITH spaces: this must wrap at the
    // word boundaries inside the sentence, not push the paragraph wide.
    { kind: "message", uuid: "r2", ts: iso(856000), role: "assistant", sidechain: false, model: "glm-5.3",
      text: "Danach `n-control build --no-cache frontend` ausfuehren und das Ergebnis in `deploy/frontend-container.log` pruefen." },
    // A real GFM table with prose cells — must scroll in its own box.
    { kind: "message", uuid: "r3", ts: iso(854000), role: "assistant", sidechain: false, model: "glm-5.3",
      text: [
        "| Agent | Letzte Aeusserung | Status |",
        "| --- | --- | --- |",
        "| Rex | letzte echte Aeusserung vor 1 min | wartet auf Rueckmeldung des Boss-Hosts |",
        "| Hermes | heartbeat ok, keine Tickets offen | idle |",
      ].join("\n") },
    // Hypothesis-1 probe: the SAME shapes inside the user bubble — a flex
    // child sized by shrink-to-fit (max-w-[85%]), where an `overflow-x-auto`
    // wrapper without `min-w-0` could still grow the bubble via its
    // intrinsic min-content.
    { kind: "message", uuid: "r4", ts: iso(852000), role: "user", sidechain: false, model: null,
      text: [
        "Kontext:",
        "",
        "```",
        "docker compose --profile n-control build --no-cache frontend",
        "```",
        "",
        "| Agent | Letzte Aeusserung | Status |",
        "| --- | --- | --- |",
        "| Rex | letzte echte Aeusserung vor 1 min | wartet auf Rueckmeldung |",
      ].join("\n") },
    { kind: "message", uuid: "r5", ts: iso(850000), role: "user", sidechain: false, model: null,
      text: "Bitte `n-control build --no-cache frontend` anwerfen und `deploy/frontend-container.log` pruefen." },
    // Allowed to be wide: a fenced code block and a markdown table each carry
    // their own horizontal scroller, so their content never reaches the
    // transcript box.
    { kind: "message", uuid: "w5", ts: iso(860000), role: "assistant", sidechain: false, model: "claude-opus-4", text: [
        `Siehe \`${LONG_PATH}\`.`,
        "",
        "```bash",
        "printf '%s\\n' \"eine absichtlich breite zeile im codeblock die scrollen darf\"",
        "```",
        "",
        "| Agent | Notiz |",
        "| --- | --- |",
        `| boss-host | ${UNBREAKABLE} |`,
      ].join("\n") },
  ];
  // Enough tail that the transcript actually scrolls vertically.
  for (let i = 0; i < 20; i++) {
    events.push({ kind: "message", uuid: `t${i}`, ts: iso(800000 - i * 1000), role: "assistant",
      sidechain: false, model: null, text: `Zeile ${i}` });
  }
  return events;
}

async function buildFixture() {
  // The stylesheet step is load-bearing: without a real Tailwind pass the
  // guard would measure a page with no utilities and pass on a broken tree.
  const cssPath = path.join(ROOT, "src/styles/globals.css");
  const compiled = await postcss([tailwind()]).process(fs.readFileSync(cssPath, "utf-8"), {
    from: cssPath,
    to: path.join(DIST, "globals.css"),
  });

  const res = await build({
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
      lib: { entry: path.join(HERE, "chat-width-entry.tsx"), formats: ["es"], fileName: "app" },
      rollupOptions: { output: { inlineDynamicImports: true } },
    },
  });

  const appFile = fs.readdirSync(DIST).find((f) => /^app\.(m?js)$/.test(f));
  if (!appFile) throw new Error("no bundle produced in " + DIST);
  const appJs = fs.readFileSync(path.join(DIST, appFile), "utf-8");
  // `process` must exist before the module runs: the bundle's own
  // `process.env.*` reads happen at import time.
  fs.writeFileSync(path.join(DIST, "index.html"), `<!doctype html>
<html lang="en" class="dark" style="color-scheme: dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<style>${compiled.css}</style></head>
<body class="font-sans antialiased bg-[var(--color-bg-deep)] text-[var(--color-text-primary)] min-h-[100dvh] overflow-x-hidden">
<div id="root"></div>
<script>window.process = { env: { NODE_ENV: "production" }, platform: "linux", version: "" };</script>
<script type="module">import { mount } from "./${appFile}"; mount();</script>
</body></html>`);
  return res;
}

const MIME = { ".html": "text/html", ".mjs": "text/javascript", ".js": "text/javascript",
  ".css": "text/css", ".json": "application/json", ".woff2": "font/woff2", ".ttf": "font/ttf",
  ".ico": "image/x-icon", ".png": "image/png" };

function serve() {
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      const url = new URL(req.url, "http://x");
      let p = path.join(DIST, decodeURIComponent(url.pathname));
      if (url.pathname === "/" || !fs.existsSync(p) || fs.statSync(p).isDirectory()) {
        p = path.join(DIST, "index.html");
      }
      res.writeHead(200, { "content-type": MIME[path.extname(p)] ?? "application/octet-stream" });
      res.end(fs.readFileSync(p));
    });
    server.listen(0, "127.0.0.1", () => resolve(server));
  });
}

/** Widths the operator's phone class covers; 390 = iPhone 13 portrait. */
const VIEWPORTS = [
  { label: "390x664 (iPhone 13)", width: 390, height: 664, device: "iPhone 13" },
  { label: "400x860", width: 400, height: 860, device: null },
];
const SHELLS = ["sessions", "app"];

await buildFixture();
const server = await serve();
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch();
const failures = [];
const rows = [];

for (const vp of VIEWPORTS) {
  for (const shell of SHELLS) {
    const ctx = await browser.newContext(
      vp.device ? { ...devices[vp.device] } : { viewport: { width: vp.width, height: vp.height }, deviceScaleFactor: 3, isMobile: true, hasTouch: true },
    );
    await ctx.route("**/api/v1/agents/**/chat/history*", (r) =>
      r.fulfill({ status: 200, contentType: "application/json",
        body: JSON.stringify({ events: fixture(),
          session: { sessionId: "s1", live: true, startedAt: iso(900000), aliveness: "active" },
          hasMore: false, capabilities: null, subagentRuns: [] }) }));
    await ctx.route("**/api/v1/agents/**/chat/stream*", (r) => r.fulfill({ status: 204, body: "" }));

    const page = await ctx.newPage();
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(String(e).slice(0, 200)));
    await page.goto(origin, { waitUntil: "load" });
    await page.waitForFunction(() => typeof window.__mount === "function", { timeout: 30000 });
    await page.evaluate((s) => window.__mount(s), shell);
    await page.waitForFunction(() => !!document.querySelector('[data-testid="chat-timeline"] *'), { timeout: 30000 });
    await page.waitForTimeout(1000);

    const m = await page.evaluate(() => window.__measure());
    const label = `${shell} @ ${vp.label}`;
    rows.push({ label, over: m.scroller?.overflow, leaks: m.leaks.length, pageErrors: pageErrors.length });

    if (!m.scroller) failures.push(`${label}: no transcript scroller found`);
    if (m.scroller && m.scroller.overflow !== 0) {
      failures.push(`${label}: transcript scrolls sideways by ${m.scroller.overflow}px ` +
        `(scrollWidth ${m.scroller.scrollWidth} vs clientWidth ${m.scroller.clientWidth})`);
    }
    for (const leak of m.leaks) {
      failures.push(`${label}: <${leak.tag}${leak.testid ? ` data-testid="${leak.testid}"` : ""}> ` +
        `overflows by ${leak.over}px with overflow-wrap:${leak.overflowWrap} — "${leak.text}"`);
    }
    if (pageErrors.length) failures.push(`${label}: page errors: ${pageErrors.join(" | ")}`);

    // The contract also has to survive a real gesture: if the box CAN scroll
    // sideways, a horizontal drag will move it. A rect-only check can miss a
    // box that merely reports equal widths at rest.
    const cdp = await ctx.newCDPSession(page);
    const box = await page.evaluate(() => {
      const r = document.querySelector('[data-testid="chat-timeline"]').parentElement.getBoundingClientRect();
      return { cx: r.left + r.width / 2, cy: r.top + r.height / 2 };
    });
    const touch = (type, x, y) => cdp.send("Input.dispatchTouchEvent", { type,
      touchPoints: type === "touchEnd" ? [] : [{ x, y, id: 1, radiusX: 14, radiusY: 14, force: 1 }] });
    await touch("touchStart", box.cx, box.cy);
    for (let i = 1; i <= 10; i++) { await touch("touchMove", box.cx - i * 20, box.cy); await page.waitForTimeout(10); }
    await touch("touchEnd", box.cx - 200, box.cy);
    await page.waitForTimeout(300);
    const afterPan = await page.evaluate(() => {
      const sc = document.querySelector('[data-testid="chat-timeline"]').parentElement;
      return { scrollLeft: sc.scrollLeft, scrollTop: sc.scrollTop };
    });
    if (afterPan.scrollLeft !== 0) {
      failures.push(`${label}: a horizontal touch drag moved the transcript ${afterPan.scrollLeft}px sideways`);
    }
    if (afterPan.scrollTop === 0) {
      failures.push(`${label}: a vertical touch drag did not move the transcript at all`);
    }
    await ctx.close();
  }
}

await browser.close();
server.close();
fs.rmSync(DIST, { recursive: true, force: true });

console.log("\n  transcript width contract");
for (const r of rows) {
  console.log(`  ${failures.some((f) => f.startsWith(r.label)) ? "FAIL" : "ok  "}  ${r.label}  ` +
    `over=${r.over}px leaks=${r.leaks} pageErrors=${r.pageErrors}`);
}

if (failures.length) {
  console.error(`\n${failures.length} violation(s):`);
  for (const f of failures) console.error("  - " + f);
  process.exit(1);
}
console.log("\nall viewports: transcript scrollWidth == clientWidth, no leaks, gesture inert sideways.\n");

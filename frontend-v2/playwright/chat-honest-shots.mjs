/**
 * Pictures of the chat package of 31.10.2026 — the live row while the engine
 * works without streaming, a genuinely silent row, a normal row, the session
 * dividers and the sticky task head on a phone.
 *
 * Builds the REAL `ChatView` (`chat-honest-entry.tsx`) with the real Tailwind
 * pass, serves it on a throwaway port and answers the chat endpoints with
 * synthetic data per scene. No backend, no login.
 *
 * Aufruf (aus frontend-v2/):
 *   OUT=/path/to/dir node playwright/chat-honest-shots.mjs
 * Optional: LOCALE=en, DIST=/path (build dir; default under OUT).
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
const OUT = process.env.OUT ?? path.join(HERE, ".chat-honest-shots");
const DIST = process.env.DIST ?? path.join(OUT, ".dist");
const LOCALE = process.env.LOCALE ?? "de";
const TASK_ID = "00000000-0000-4000-8000-000000000001";
fs.mkdirSync(OUT, { recursive: true });

const now = Date.now();
const ago = (sec) => new Date(now - sec * 1000).toISOString();

const msg = (uuid, sec, role, text) => ({ kind: "message", uuid, ts: ago(sec), role, text, model: null, sidechain: false });
let rowN = 0;
const think = (sec, lead) => ({ id: `1:${++rowN}`, ts: ago(sec), k: "think", lead });
const tool = (sec, title) => ({ id: `1:${++rowN}`, ts: ago(sec), k: "tool", name: "bash", title, st: "done" });

function workRows(fromSec, toSec, n) {
  const leads = [
    "Read the brief and the reference files first.",
    "The page needs one HTML file with the scene inline.",
    "Checked the asset folder for the 3D library.",
    "The library is there as a UMD build, inline-safe.",
    "Plan: scene, ship controls, asteroid field, score.",
    "Writing the head part of the page now.",
  ];
  const tools = ["$ ls -la /workspace/bench/", "$ grep -rl \"three\" /deliverables/", "$ wc -c three.min.js", "$ mc checklist add \"Build scene\""];
  const rows = [];
  for (let i = 0; i < n; i++) {
    const sec = fromSec - ((fromSec - toSec) * i) / Math.max(1, n - 1);
    rows.push(think(sec, leads[i % leads.length]));
    rows.push(tool(sec - 5, tools[i % tools.length]));
  }
  return rows;
}

function scene(name) {
  rowN = 0;
  const stub = {
    taskId: TASK_ID, title: "[Bench] Minigame — local model", status: "in_progress", live: true,
    deliveredAt: ago(1820), startedAt: ago(1800), startKey: ago(1800), endedAt: null, assignee: null,
    outcome: null, deliverables: 0,
  };
  const events = [msg("m1", 1900, "user", "Kannst du den Bench-Lauf im Auge behalten?"), msg("m2", 1890, "assistant", "Ja — ich melde mich, sobald er fertig ist.")];
  let rows = workRows(1700, 220, 6);
  let liveRow = null;
  let engine = null;
  if (name.startsWith("live")) {
    const quiet = name !== "live-normal";
    liveRow = { block: "thinking", startedAt: ago(201), ts: ago(quiet ? 142 : 2), chars: 29_200, stale: false };
    if (name === "live-busy") engine = { busy: true, running: 1, checkedAt: ago(1) };
    if (name === "live-quiet") engine = { busy: false, running: 0, checkedAt: ago(1) };
  } else {
    // dividers / header: chat question in the middle of the task, an answer from the chat, more task work
    rows = [...workRows(1700, 700, 12), ...workRows(500, 260, 3)];
    events.push(msg("m3", 600, "user", "Wie weit bist du?"));
    events.push(msg("m4", 590, "assistant", "Die Aufgabe läuft in ihrer eigenen Session — Szene steht, jetzt die Steuerung."));
  }
  const steps = {
    applies: true, taskId: TASK_ID, status: "in_progress", live: true, hasSession: true,
    attempts: [{ n: 1, startedAt: ago(1800) }], rows, updated: [], cursor: `v1.${rowN}`, hasMore: false,
    reset: false, omitted: 0, liveRow,
  };
  return { stub, events, steps, engine, liveRow };
}

async function buildFixture() {
  const cssPath = path.join(ROOT, "src/styles/globals.css");
  const compiled = await postcss([tailwind()]).process(fs.readFileSync(cssPath, "utf-8"), {
    from: cssPath, to: path.join(DIST, "globals.css"),
  });
  await build({
    root: ROOT, configFile: false, logLevel: "error", plugins: [react()],
    resolve: { alias: { "@": path.join(ROOT, "src") } },
    define: { "process.env.NODE_ENV": '"production"' },
    build: {
      outDir: DIST, emptyOutDir: true, minify: false,
      lib: { entry: path.join(HERE, "chat-honest-entry.tsx"), formats: ["es"], fileName: "app" },
      rollupOptions: { output: { inlineDynamicImports: true } },
    },
  });
  const appFile = fs.readdirSync(DIST).find((f) => /^app\.(m?js)$/.test(f));
  fs.writeFileSync(path.join(DIST, "index.html"), `<!doctype html>
<html lang="${LOCALE}" class="dark" style="color-scheme: dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<style>${compiled.css}</style></head>
<body class="font-sans antialiased bg-[var(--color-bg-deep)] text-[var(--color-text-primary)] min-h-[100dvh] overflow-x-hidden">
<div id="root"></div>
<script>window.process = { env: { NODE_ENV: "production" }, platform: "linux", version: "" };</script>
<script type="module">import { mount } from "./${appFile}"; mount();</script>
</body></html>`);
}

function serve() {
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      const url = new URL(req.url, "http://x");
      let p = path.join(DIST, decodeURIComponent(url.pathname));
      if (url.pathname === "/" || !fs.existsSync(p) || fs.statSync(p).isDirectory()) p = path.join(DIST, "index.html");
      const type = { ".html": "text/html", ".mjs": "text/javascript", ".js": "text/javascript", ".css": "text/css" }[path.extname(p)];
      res.writeHead(200, { "content-type": type ?? "application/octet-stream" });
      res.end(fs.readFileSync(p));
    });
    server.listen(0, "127.0.0.1", () => resolve(server));
  });
}

const json = (r, body) => r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

const SHOTS = [
  { scene: "live-normal", vp: "phone" }, { scene: "live-busy", vp: "phone" }, { scene: "live-quiet", vp: "phone" },
  { scene: "live-normal", vp: "desk" }, { scene: "live-busy", vp: "desk" }, { scene: "live-quiet", vp: "desk" },
  { scene: "dividers", vp: "phone" }, { scene: "dividers", vp: "desk" },
  { scene: "header", vp: "phone" }, { scene: "header", vp: "desk" },
];
const VIEWPORTS = {
  phone: { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true },
  desk: { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 },
};

await buildFixture();
const server = await serve();
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch();
const report = [];

for (const shot of SHOTS) {
  const data = scene(shot.scene);
  const ctx = await browser.newContext(VIEWPORTS[shot.vp]);
  await ctx.route("**/api/v1/**", (r) => {
    const u = new URL(r.request().url());
    const p = u.pathname;
    if (p.endsWith("/chat/history")) {
      return json(r, { events: data.events, session: { sessionId: "s1", live: true, startedAt: ago(4000), aliveness: "active" }, hasMore: false, capabilities: null, subagentRuns: [] });
    }
    if (p.endsWith("/chat/stream")) return r.fulfill({ status: 204, body: "" });
    if (p.endsWith("/chat/tasks")) return json(r, { applies: true, tasks: [data.stub], queued: 0, hasMore: false });
    if (p.endsWith("/steps/live")) return json(r, { liveRow: data.liveRow ? { ...data.liveRow, text: "…the call writes the whole page in one go." } : null });
    if (p.endsWith("/steps")) return json(r, data.steps);
    if (p.endsWith("/chat/engine")) return json(r, { engine: data.engine });
    return r.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e).slice(0, 200)));
  await page.goto(origin, { waitUntil: "load" });
  await page.waitForFunction(() => typeof window.__mountHonest === "function", { timeout: 30000 });
  await page.evaluate((l) => window.__mountHonest(l), LOCALE);
  await page.waitForSelector('[data-testid="chat-task-segment"]', { timeout: 30000 });
  await page.waitForTimeout(1500);
  let measure = null;
  if (shot.scene === "header") {
    // Scroll so the first band's head is stuck while its rows pass beneath it.
    measure = await page.evaluate(() => {
      const scroller = document.querySelector('[data-testid="chat-timeline"]').parentElement;
      const seg = document.querySelector('[data-testid="chat-task-segment"]');
      scroller.scrollTop = seg.offsetTop + 420;
      return null;
    });
    await page.waitForTimeout(400);
    measure = await page.evaluate(() => {
      const scroller = document.querySelector('[data-testid="chat-timeline"]').parentElement;
      const head = document.querySelector('[data-testid="chat-task-head"]');
      const s = scroller.getBoundingClientRect();
      const h = head.getBoundingClientRect();
      // Text visible between the scroller's top edge and the stuck head = a peek.
      return { scrollerTop: Math.round(s.top), headTop: Math.round(h.top), gap: Math.round(h.top - s.top) };
    });
  }
  const file = path.join(OUT, `${shot.scene}-${shot.vp}.png`);
  await page.screenshot({ path: file });
  const row = shot.scene.startsWith("live")
    ? await page.evaluate(() => document.querySelector('[data-testid="live-thinking"]')?.textContent ?? null)
    : null;
  report.push({ shot: `${shot.scene}-${shot.vp}`, file, row, measure, errors });
  await ctx.close();
}

await browser.close();
server.close();
console.log(JSON.stringify(report, null, 2));
// The stuck head must sit flush on the scroller's top edge: any gap shows
// the rows passing beneath it (phone finding 31.10.2026).
const bad = report.filter((r) => (r.measure && r.measure.gap !== 0) || r.errors.length);
if (bad.length) {
  console.error("FAIL:", bad.map((r) => `${r.shot} gap=${r.measure?.gap} errors=${r.errors.length}`).join("; "));
  process.exit(1);
}

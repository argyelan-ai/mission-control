/**
 * Pictures of the ADR-100 phone view (stage 2d): the inbox tabs "For you" /
 * "Between agents" (with data and empty), an opened conversation (running
 * and brake-paused), the directory sheet from the chat header, the send card
 * in the chat and an approval card with "triggered by a message from X".
 *
 * Builds the REAL inbox page and `ChatView` (`comms-phone-entry.tsx`) with
 * the real Tailwind pass, serves them on a throwaway port and answers the
 * endpoints with synthetic data. No backend, no login, no live database.
 *
 * Aufruf (aus frontend-v2/):
 *   OUT=/path/to/dir node playwright/comms-phone-shots.mjs
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
const OUT = process.env.OUT ?? path.join(HERE, ".comms-phone-shots");
const DIST = process.env.DIST ?? path.join(OUT, ".dist");
fs.mkdirSync(OUT, { recursive: true });

const now = Date.now();
const ago = (sec) => new Date(now - sec * 1000).toISOString();

// ── synthetic data ─────────────────────────────────────────────────────────

const LEAD = { ref: "agent:1", kind: "agent", address: "lead", label: "Lead", harness: "claude" };
const WORKER = { ref: "agent:2", kind: "agent", address: "worker", label: "Worker", harness: "omp" };
const HELPER = { ref: "agent:3", kind: "agent", address: "helper", label: "Helper", harness: "hermes" };
const TERM = { ref: "session:4", kind: "session", address: "claude@desk:runtimes", label: "runtimes", harness: "claude" };

const MESSAGES = {
  "th-1": [
    { id: "m1", seq: 1, from: "worker", text: "When is the hand test on box one done?", created_at: ago(1500) },
    { id: "m2", seq: 2, from: "claude@desk:runtimes", text: "Still measuring, done at about 15:30.\nAfter that I set the runtime back to active.", created_at: ago(1380) },
  ],
  "th-2": [
    { id: "p1", seq: 1, from: "lead", text: "Can you rerun the bench on the small model?", created_at: ago(5400) },
    { id: "p2", seq: 2, from: "worker", text: "Started. First numbers in ten minutes.", created_at: ago(5300) },
    { id: "p3", seq: 3, from: "lead", text: "Thanks!", created_at: ago(5200) },
    { id: "p4", seq: 4, from: "worker", text: "You're welcome.", created_at: ago(5150) },
    { id: "p5", seq: 5, from: "lead", text: "Ping me when it is done.", created_at: ago(5100) },
    { id: "p6", seq: 6, from: "worker", text: "Will do.", created_at: ago(5050) },
  ],
  "th-3": [{ id: "s1", seq: 1, from: "helper", text: "Shall I clean the old images?", created_at: ago(90000) }],
};

const conv = (id, a, b, state, extra = {}) => {
  const msgs = MESSAGES[id];
  const last = msgs[msgs.length - 1];
  return {
    thread_id: id, participants: [a, b], message_count: msgs.length, last_message: last,
    last_message_at: last.created_at, state, brake_approval_id: state === "running" ? null : "ap-brake", ...extra,
  };
};
const CONVERSATIONS = [
  conv("th-1", WORKER, TERM, "running"),
  conv("th-2", LEAD, WORKER, "paused"),
  conv("th-3", HELPER, LEAD, "stopped"),
];

const approval = (o) => ({
  board_id: "b", task_id: null, agent_id: "agent-2", payload: null, confidence: null, status: "pending",
  autonomy_level: null, resolved_at: null, resolver_note: null, failure_reason: null, expires_at: null,
  triggered_by: null, ...o,
});
const APPROVALS = [
  approval({
    id: "ap-1", action_type: "config_change", created_at: ago(600),
    description: "Stop the engine on box one and switch it to the small model.",
    triggered_by: { from: "claude@desk:runtimes", thread_id: "th-1", message_id: "m2", at: ago(700) },
  }),
  approval({
    id: "ap-brake", action_type: "comms_continue", created_at: ago(5040),
    description: "lead and worker exchanged 6 messages without you. Let the conversation continue?",
    payload: { thread_id: "th-2", between: ["lead", "worker"] },
  }),
];

const dirRow = (o) => ({
  kind: "agent", machine: null, alive: "yes", listening: true, busy: false, accept: "accept", task: null,
  last_seen: ago(12), ...o,
});
const DIRECTORY = {
  warming_up: false,
  sessions: [
    dirRow({ address: "worker", label: "Worker", harness: "omp", busy: true, task: "box question to runtimes", last_seen: ago(12) }),
    dirRow({ address: "claude@desk:runtimes", kind: "session", label: "runtimes", harness: "claude", machine: "desk", task: "hand test on box one", last_seen: ago(40) }),
    dirRow({ address: "lead", label: "Lead", harness: "claude", last_seen: ago(60) }),
    dirRow({ address: "helper", label: "Helper", harness: "hermes", listening: false, last_seen: ago(25) }),
  ],
};

const msg = (uuid, sec, role, text) => ({ kind: "message", uuid, ts: ago(sec), role, text, model: null, sidechain: false });
const tool = (uuid, sec, name, title, detail, result) => ({
  kind: "tool", uuid, ts: ago(sec), name, title, detail, toolUseId: `tu-${uuid}`, result, status: "done", stats: null, sidechain: false,
});
const CHAT_EVENTS = [
  msg("c1", 1700, "user", "Ask runtimes when the hand test on box one is done."),
  tool("c2", 1650, "bash", "$ mc who", { command: "mc who" }, "claude@desk:runtimes · session · claude · alive yes · listening yes"),
  tool("c3", 1600, "bash", '$ mc send claude@desk:runtimes "When is the hand test on box one done?"',
    { command: 'mc send claude@desk:runtimes "When is the hand test on box one done?"' },
    "sent to claude@desk:runtimes · next_rest"),
  msg("c4", 1590, "assistant", "Asked runtimes. I'll tell you as soon as the answer is in."),
  msg("c5", 1300, "assistant", "runtimes: still measuring, done at about 15:30. Box two is free to use now."),
];

// ── build + serve ─────────────────────────────────────────────────────────

async function buildFixture() {
  const cssPath = path.join(ROOT, "src/styles/globals.css");
  const compiled = await postcss([tailwind()]).process(fs.readFileSync(cssPath, "utf-8"), {
    from: cssPath, to: path.join(DIST, "globals.css"),
  });
  const stubs = path.join(HERE, "comms-phone-stubs.tsx");
  await build({
    root: ROOT, configFile: false, logLevel: "error", plugins: [react()],
    resolve: {
      alias: [
        { find: /^next\/link$/, replacement: stubs },
        { find: /^next\/navigation$/, replacement: stubs },
        { find: /^@\/components\/layout\/AppShell$/, replacement: path.join(HERE, "comms-phone-appshell.tsx") },
        { find: /^@\//, replacement: path.join(ROOT, "src") + "/" },
      ],
    },
    define: { "process.env.NODE_ENV": '"production"' },
    build: {
      outDir: DIST, emptyOutDir: true, minify: false,
      lib: { entry: path.join(HERE, "comms-phone-entry.tsx"), formats: ["es"], fileName: "app" },
      rollupOptions: { output: { inlineDynamicImports: true } },
    },
  });
  const appFile = fs.readdirSync(DIST).find((f) => /^app\.(m?js)$/.test(f));
  fs.writeFileSync(path.join(DIST, "index.html"), `<!doctype html>
<html lang="en" class="dark" style="color-scheme: dark"><head><meta charset="utf-8">
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

// ── shots ─────────────────────────────────────────────────────────────────

const VIEWPORTS = {
  phone: { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true },
  desk: { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 },
};

/** name, view, query, locale, viewport, data overrides, an action, what must be visible. */
const SHOTS = [
  { name: "01-inbox-for-you", view: "inbox", q: "", wait: '[data-testid="approval-triggered-by"]' },
  { name: "02-inbox-for-you-empty", view: "inbox", q: "", empty: true, wait: '[data-testid="inbox-tabs"]' },
  { name: "03-inbox-between-agents", view: "inbox", q: "?tab=agents", wait: '[data-testid="conversation-row"]' },
  { name: "04-inbox-between-agents-empty", view: "inbox", q: "?tab=agents", empty: true, wait: '[data-testid="between-empty"]' },
  { name: "05-conversation", view: "inbox", q: "?tab=agents&c=th-1", wait: '[data-testid="conversation-message"]' },
  { name: "06-conversation-brake-paused", view: "inbox", q: "?tab=agents&c=th-2", wait: '[data-testid="conversation-decide"]', full: true },
  { name: "07-chat-send-card", view: "chat", q: "", wait: '[data-testid="send-card"]' },
  { name: "08-directory-sheet", view: "chat", q: "", wait: '[data-testid="reachable-button"]', click: '[data-testid="reachable-button"]', after: '[data-testid="directory-row"]' },
  { name: "09-approval-triggered-by", view: "inbox", q: "", wait: '[data-testid="approval-triggered-by"]', element: '[data-testid="approval-card"]' },
  // one set in German
  { name: "10-de-inbox-between-agents", view: "inbox", q: "?tab=agents", locale: "de", wait: '[data-testid="conversation-row"]' },
  { name: "11-de-directory-sheet", view: "chat", q: "", locale: "de", wait: '[data-testid="reachable-button"]', click: '[data-testid="reachable-button"]', after: '[data-testid="directory-row"]' },
  { name: "12-de-inbox-for-you", view: "inbox", q: "", locale: "de", wait: '[data-testid="approval-triggered-by"]' },
  // desktop check
  { name: "13-desk-inbox-between-agents", view: "inbox", q: "?tab=agents", vp: "desk", wait: '[data-testid="conversation-row"]' },
  { name: "14-desk-conversation", view: "inbox", q: "?tab=agents&c=th-1", vp: "desk", wait: '[data-testid="conversation-message"]' },
  { name: "15-desk-chat-directory", view: "chat", q: "", vp: "desk", wait: '[data-testid="reachable-button"]', click: '[data-testid="reachable-button"]', after: '[data-testid="directory-row"]' },
];

await buildFixture();
const server = await serve();
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch();
const report = [];

for (const shot of SHOTS) {
  const ctx = await browser.newContext({ ...VIEWPORTS[shot.vp ?? "phone"], locale: shot.locale === "de" ? "de-DE" : "en-GB", timezoneId: "UTC" });
  await ctx.addInitScript(() => {
    localStorage.setItem("mc_auth_token", "shot");
    localStorage.setItem("mc_user", JSON.stringify({ id: "u1", email: "op@example.invalid", name: "Operator", role: "admin" }));
  });
  await ctx.route("**/api/v1/**", (r) => {
    const p = new URL(r.request().url()).pathname;
    if (p.endsWith("/approvals")) return json(r, shot.empty ? [] : APPROVALS);
    if (p.endsWith("/comms/conversations")) return json(r, { conversations: shot.empty ? [] : CONVERSATIONS });
    const m = /\/comms\/conversations\/([^/]+)\/messages$/.exec(p);
    if (m) {
      const c = CONVERSATIONS.find((x) => x.thread_id === m[1]);
      return json(r, { conversation: c, messages: MESSAGES[m[1]] });
    }
    if (p.endsWith("/comms/directory")) return json(r, DIRECTORY);
    if (p.endsWith("/heads") || p.includes("/heads?")) return json(r, { runs: [] });
    if (p.endsWith("/agents")) return json(r, []);
    if (p.endsWith("/chat/history")) {
      return json(r, { events: CHAT_EVENTS, session: { sessionId: "s1", live: true, startedAt: ago(4000), aliveness: "idle" }, hasMore: false, capabilities: null, subagentRuns: [] });
    }
    if (p.endsWith("/chat/stream")) return r.fulfill({ status: 204, body: "" });
    return r.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e).slice(0, 200)));
  await page.goto(`${origin}/inbox${shot.q}`, { waitUntil: "load" });
  await page.waitForFunction(() => typeof window.__mountComms === "function", { timeout: 30000 });
  await page.evaluate(([v, l]) => window.__mountComms(v, l), [shot.view, shot.locale ?? "en"]);
  await page.waitForSelector(shot.wait, { timeout: 30000 });
  if (shot.click) {
    await page.click(shot.click);
    await page.waitForSelector(shot.after, { timeout: 10000 });
  }
  await page.waitForTimeout(700);
  const file = path.join(OUT, `${shot.name}.png`);
  if (shot.element) await page.locator(shot.element).first().screenshot({ path: file });
  else await page.screenshot({ path: file, fullPage: !!shot.full });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  report.push({ shot: shot.name, file, overflow, errors });
  await ctx.close();
}

await browser.close();
server.close();
console.log(JSON.stringify(report, null, 2));
const bad = report.filter((r) => r.overflow || r.errors.length);
if (bad.length) {
  console.error("FAIL:", bad.map((r) => `${r.shot} overflow=${r.overflow} errors=${r.errors.join(" | ")}`).join("; "));
  process.exit(1);
}

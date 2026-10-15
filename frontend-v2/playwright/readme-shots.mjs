/**
 * README screenshots — real pages of a throwaway demo install, chat content
 * from invented fixtures (`readme-fixtures.mjs`).
 *
 * Never point this at a real install: it logs in with the demo admin and the
 * chat routes are answered from fixtures, but every other page shows that
 * instance's own data. How to bring up the demo stack and run this:
 * scripts/readme-shots/README.md.
 *
 *   node playwright/readme-shots.mjs [shot ...]     (from frontend-v2/)
 *
 * Env: DEMO_URL (default http://127.0.0.1:13000), DEMO_EMAIL / DEMO_PASSWORD,
 * SHOT_OUT (default ../docs/assets). Without arguments every shot is taken.
 */
import { chromium } from "playwright";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as fx from "./readme-fixtures.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BASE = process.env.DEMO_URL ?? "http://127.0.0.1:13000";
const OUT = path.resolve(HERE, process.env.SHOT_OUT ?? "../../docs/assets");
const EMAIL = process.env.DEMO_EMAIL ?? "operator@example.com";
const PASSWORD = process.env.DEMO_PASSWORD ?? "demo-password-123";
const FRAMES = process.env.FRAMES_OUT ?? path.join(HERE, ".readme-frames");

const DESKTOP = { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 };
const PHONE = { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true };

async function login() {
  const r = await fetch(`${BASE}/api/v1/auth/login`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email: EMAIL, password: PASSWORD }),
  });
  if (!r.ok) throw new Error(`demo login failed (${r.status}) — is the demo stack up and the admin created?`);
  return r.json();
}

const json = (route, body, status = 200) =>
  route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

/**
 * Chat fixtures: the two agents and everything their chats ask for. The SSE
 * stream is held open until `push()` hands it events — that is how the GIF
 * shows the agent carrying on after a tap.
 */
function chatRoutes(ctx, opts = {}) {
  const state = { forgeStage: opts.forgeStage ?? "ask", waiting: [], pending: [] };
  const agents = [fx.LEAD, fx.FORGE];

  ctx.route("**/api/v1/docker-sessions/agents", (r) => json(r, [fx.FORGE]));
  ctx.route("**/api/v1/host-sessions/agents", (r) => json(r, [fx.LEAD]));
  ctx.route("**/api/v1/groups*", (r) => json(r, []));

  for (const a of agents) {
    const base = `**/api/v1/agents/${a.id}`;
    const isLead = a.id === fx.LEAD.id;
    ctx.route(`${base}/chat/history*`, (r) =>
      json(r, {
        events: isLead ? [...fx.leadEvents(), fx.LEAD_TODOS] : [...fx.forgeEvents(state.forgeStage), fx.FORGE_USAGE],
        session: { sessionId: `s-${a.slug}`, live: true, startedAt: fx.iso(3_600_000), aliveness: "active" },
        hasMore: false,
        subagentRuns: [],
        capabilities: isLead ? null : fx.FORGE_CAPS,
      }),
    );
    ctx.route(`${base}/chat/changes*`, (r) => json(r, isLead ? fx.leadChanges() : { files: [], stats: { files: 0, additions: 0, deletions: 0 }, repo: null }));
    ctx.route(`${base}/chat/diff*`, (r) => json(r, { detail: "no_workspace" }, 404));
    ctx.route(`${base}/chat/commit/draft*`, (r) => json(r, fx.LEAD_COMMIT_DRAFT));
    ctx.route(`${base}/chat/pr/draft*`, (r) =>
      json(r, { branch: "fix/coupon-rounding", base: "main", title: "fix(cart): apply the coupon before rounding the total", ahead: 1, uncommitted: 0 }),
    );
    ctx.route(`${base}/chat/elicitation/**`, (r) => json(r, { status: "answered" }));
    ctx.route(`${base}/chat/queue*`, (r) => json(r, { queued: [] }));
    // Anything else about these two invented agents: an empty, harmless answer.
    ctx.route(`${base}/**`, (r) => r.fallback());
  }
  // The live stream: an in-page stand-in for EventSource on the two chat
  // streams only (a held network response cannot trickle events). It sends a
  // keepalive and the agent's state at once, then whatever `push()` hands it.
  ctx.addInitScript((initial) => {
    const Real = window.EventSource;
    window.__chatStreams = [];
    class FixtureStream extends EventTarget {
      constructor(url) {
        super();
        this.url = url;
        this.withCredentials = true;
        this.readyState = 0;
        this.onopen = this.onmessage = this.onerror = null;
        const id = (url.match(/agents\/([^/]+)\/chat\/stream/) || [])[1];
        this.agentId = id;
        window.__chatStreams.push(this);
        setTimeout(() => {
          this.readyState = 1;
          this.onopen?.(new Event("open"));
          this.emit("ping", {});
          if (initial[id]) this.emit("chat_event", initial[id]);
        }, 50);
      }
      emit(type, data) {
        this.dispatchEvent(new MessageEvent(type, { data: JSON.stringify(data) }));
      }
      close() { this.readyState = 2; }
    }
    window.EventSource = function (url, init) {
      return /\/chat\/stream/.test(String(url)) ? new FixtureStream(String(url)) : new Real(url, init);
    };
    Object.assign(window.EventSource, { CONNECTING: 0, OPEN: 1, CLOSED: 2 });
  }, opts.initialState ?? {
    [fx.LEAD.id]: { kind: "state", status: "idle", prompt: null, aliveness: "active" },
    [fx.FORGE.id]: { kind: "state", status: "waiting_input", prompt: null, aliveness: "active" },
  });
  return {
    state,
    /** Hand chat events to the open streams of one agent. */
    async push(page, agentId, events) {
      await page.evaluate(([id, evs]) => {
        for (const s of window.__chatStreams) {
          if (s.agentId === id && s.readyState === 1) for (const e of evs) s.emit("chat_event", e);
        }
      }, [agentId, events]);
    },
    release() {},
  };
}

async function newContext(browser, auth, device) {
  const ctx = await browser.newContext({
    ...device,
    colorScheme: "dark",
    locale: "en-US",
    timezoneId: "UTC",
  });
  await ctx.addInitScript(([token, user]) => {
    try {
      localStorage.setItem("mc_auth_token", token);
      localStorage.setItem("mc_user", JSON.stringify(user));
      localStorage.setItem("mc_theme", "dark");
    } catch {}
  }, [auth.access_token, auth.user]);
  return ctx;
}

async function settle(page, ms = 1200) {
  await page.waitForLoadState("networkidle").catch(() => {});
  await page.waitForTimeout(ms);
}

async function shot(page, name) {
  const file = path.join(OUT, name);
  await page.screenshot({ path: file });
  console.log("  wrote", path.relative(process.cwd(), file));
}

// ── The shots ────────────────────────────────────────────────────────────────

const SHOTS = {
  /** Desktop hero: the lead agent's chat with per-edit diffs + the Changes panel. */
  async "mc-chat-desktop"(browser, auth) {
    const ctx = await newContext(browser, auth, DESKTOP);
    const chat = chatRoutes(ctx);
    await ctx.addInitScript(() => {
      try {
        localStorage.setItem("mc.chat.panel", "diff");
        localStorage.setItem("mc.chat.changestab", "chat");
        localStorage.setItem("mc.chat.detail", "normal");
        localStorage.setItem("mc.chat.sidebar", "collapsed");
      } catch {}
    });
    const page = await ctx.newPage();
    await page.goto(`${BASE}/sessions?agent=${fx.LEAD.id}`);
    await page.waitForSelector('[data-testid="chat-timeline"]', { timeout: 30000 });
    await settle(page);
    // Open the group that holds the two edits, so their diffs show.
    await page.getByText(/command executed/).first().click();
    await page.waitForTimeout(600);
    await page.getByText("Edit cart.ts", { exact: true }).first().click();
    await page.waitForTimeout(400);
    // ...and the same file in the Changes panel.
    await page.getByText("cart.ts", { exact: true }).last().click();
    await page.waitForTimeout(600);
    await page.evaluate(() => {
      const t = [...document.querySelectorAll("*")].find((e) => e.textContent?.trim() === "Edit cart.ts");
      const sc = document.querySelector('[data-testid="chat-timeline"]')?.parentElement;
      if (t && sc) sc.scrollTop += t.getBoundingClientRect().top - sc.getBoundingClientRect().top - 330;
    });
    await page.waitForTimeout(600);
    await shot(page, "mc-chat-desktop.png");
    chat.release();
    await ctx.close();
  },

  /** Changes panel: Commit with a suggested message (then Open PR). */
  async "mc-chat-commit"(browser, auth) {
    const ctx = await newContext(browser, auth, DESKTOP);
    chatRoutes(ctx);
    await ctx.addInitScript(() => {
      try {
        localStorage.setItem("mc.chat.panel", "diff");
        localStorage.setItem("mc.chat.changestab", "chat");
        localStorage.setItem("mc.chat.detail", "normal");
        localStorage.setItem("mc.chat.sidebar", "collapsed");
      } catch {}
    });
    const page = await ctx.newPage();
    await page.goto(`${BASE}/sessions?agent=${fx.LEAD.id}`);
    await page.waitForSelector('[data-testid="chat-timeline"]', { timeout: 30000 });
    await settle(page);
    await page.getByRole("button", { name: "Commit", exact: true }).click();
    await page.waitForSelector('[data-testid="commit-message"]', { timeout: 15000 });
    await page.waitForTimeout(800);
    await shot(page, "mc-chat-commit.png");
    await ctx.close();
  },

  /** Phone: the local agent asks before it plans (question card, Plan mode chip). */
  async "mc-phone-question"(browser, auth) {
    const ctx = await newContext(browser, auth, PHONE);
    chatRoutes(ctx, { forgeStage: "ask" });
    const page = await ctx.newPage();
    await page.goto(`${BASE}/sessions?agent=${fx.FORGE.id}`);
    await page.waitForSelector('[data-testid="elicitation-card"]', { timeout: 30000 });
    await settle(page);
    await page.locator('[data-testid="elicitation-card"]').last().scrollIntoViewIfNeeded();
    await page.waitForTimeout(400);
    await shot(page, "mc-phone-question.png");
    await ctx.close();
  },

  /** Phone: plan approval — approve, refine, or stop the agent. */
  async "mc-phone-plan"(browser, auth) {
    const ctx = await newContext(browser, auth, PHONE);
    chatRoutes(ctx, { forgeStage: "plan" });
    const page = await ctx.newPage();
    await page.goto(`${BASE}/sessions?agent=${fx.FORGE.id}`);
    await page.waitForSelector('[data-testid="elicitation-card"][data-status="open"]', { timeout: 30000 });
    await settle(page);
    await page.locator('[data-testid="elicitation-card"]').last().scrollIntoViewIfNeeded();
    await page.waitForTimeout(400);
    await shot(page, "mc-phone-plan.png");
    await ctx.close();
  },


  /**
   * Phone GIF: monitor + intervene. The local agent asks, the operator taps
   * the answers, the agent carries on (new edit streams in). Built from a
   * frame series (sharper and smaller than a screen recording) with ffmpeg.
   */
  async "mc-remote-phone"(browser, auth) {
    const ctx = await newContext(browser, auth, PHONE);
    const chat = chatRoutes(ctx, { forgeStage: "ask" });
    const page = await ctx.newPage();
    await page.goto(`${BASE}/sessions?agent=${fx.FORGE.id}`);
    await page.waitForSelector('[data-testid="elicitation-card"]', { timeout: 30000 });
    await settle(page);
    fs.rmSync(FRAMES, { recursive: true, force: true });
    fs.mkdirSync(FRAMES, { recursive: true });
    const frames = [];
    const frame = async (seconds) => {
      const file = path.join(FRAMES, `f${String(frames.length).padStart(3, "0")}.png`);
      await page.screenshot({ path: file });
      frames.push({ file, seconds });
    };
    // A soft dot where the finger lands, so the tap reads in a silent GIF.
    const tap = async (locator) => {
      const box = await locator.boundingBox();
      await page.evaluate(({ x, y }) => {
        const d = document.createElement("div");
        d.id = "__tap";
        Object.assign(d.style, { position: "fixed", left: `${x - 22}px`, top: `${y - 22}px`, width: "44px", height: "44px",
          borderRadius: "50%", background: "rgba(255,255,255,0.28)", border: "2px solid rgba(255,255,255,0.7)",
          zIndex: 99999, pointerEvents: "none" });
        document.body.appendChild(d);
      }, { x: box.x + box.width / 2, y: box.y + box.height / 2 });
      await frame(0.5);
      await locator.click();
      await page.evaluate(() => document.getElementById("__tap")?.remove());
      await page.waitForTimeout(250);
    };
    await frame(2.2);
    const choice = (name) => page.locator('[data-testid="elicitation-choice"]', { hasText: name }).first();
    await tap(choice("Signed cookie"));
    await frame(0.5);
    await tap(choice("lint"));
    await tap(choice("unit tests"));
    await frame(0.6);
    await tap(page.locator('[data-testid="elicitation-send"]'));
    await frame(0.9);
    // The agent carries on: its answer lands, it writes a file (live diff).
    await chat.push(page, fx.FORGE.id, [{ kind: "state", status: "working", prompt: null, aliveness: "active" }, ...fx.forgeContinues().slice(0, 2)]);
    await page.waitForTimeout(500);
    await page.evaluate(() => {
      const sc = document.querySelector('[data-testid="chat-timeline"]')?.parentElement;
      if (sc) sc.scrollTop = sc.scrollHeight;
    });
    await page.waitForTimeout(400);
    await frame(1.6);
    await page.getByText("Write session.ts", { exact: true }).first().click().catch(() => {});
    await page.waitForTimeout(400);
    await page.evaluate(() => {
      const sc = document.querySelector('[data-testid="chat-timeline"]')?.parentElement;
      if (sc) sc.scrollTop = sc.scrollHeight;
    });
    await frame(1.4);
    await chat.push(page, fx.FORGE.id, fx.forgeContinues().slice(2));
    await page.waitForTimeout(500);
    await page.evaluate(() => {
      const sc = document.querySelector('[data-testid="chat-timeline"]')?.parentElement;
      if (sc) sc.scrollTop = sc.scrollHeight;
    });
    await page.waitForTimeout(300);
    await frame(3.2);
    await ctx.close();

    // frames → GIF (two-pass palette keeps the dark UI clean and small)
    const list = path.join(FRAMES, "list.txt");
    fs.writeFileSync(list, frames.map((f) => `file '${f.file}'\nduration ${f.seconds}`).join("\n") + `\nfile '${frames.at(-1).file}'\n`);
    const gif = path.join(OUT, "mc-remote-phone.gif");
    const filters = "scale=780:-1:flags=lanczos";
    execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", list,
      "-vf", `${filters},palettegen=max_colors=96:stats_mode=full`, path.join(FRAMES, "pal.png")]);
    execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", list, "-i", path.join(FRAMES, "pal.png"),
      "-lavfi", `${filters},fps=10[x];[x][1:v]paletteuse=dither=none`, "-loop", "0", gif]);
    console.log("  wrote", path.relative(process.cwd(), gif), `${(fs.statSync(gif).size / 1e6).toFixed(2)} MB`);
    fs.rmSync(FRAMES, { recursive: true, force: true });
  },

  /**
   * Phone: optional voice call with the lead agent (OpenAI Realtime, own key,
   * daily budget). No real call: microphone and WebRTC are stood in for in
   * the page, the voice's lines are fed in as Realtime events.
   */

  /** Runtimes: your own boxes first — what each one serves right now. */
  async "mc-runtimes"(browser, auth) {
    const ctx = await newContext(browser, auth, DESKTOP);
    const hosts = fx.demoHosts();
    await ctx.route("**/api/v1/hosts", (r) => (r.request().method() === "GET" ? json(r, hosts) : r.fallback()));
    await ctx.route("**/api/v1/runtimes", async (r) => {
      if (r.request().method() !== "GET") return r.fallback();
      const real = await (await r.fetch()).json();
      json(r, { runtimes: fx.demoRuntimes(real.runtimes, hosts) });
    });
    await ctx.route("**/api/v1/runtimes/live-status*", (r) => json(r, fx.demoLiveStatus()));
    // Per-box telemetry for the two invented boxes.
    for (const [i, h] of hosts.entries()) {
      const at = `**/api/v1/hosts/${h.id}`;
      await ctx.route(`${at}/metrics`, (r) => json(r, fx.demoHostMetrics(i)));
      await ctx.route(`${at}/metrics/history*`, (r) => json(r, fx.demoHostHistory(i)));
      await ctx.route(`${at}/pulse*`, (r) => json(r, fx.demoPulse(i)));
      await ctx.route(`${at}/recipes*`, (r) => json(r, []));
      await ctx.route(`${at}/autostart*`, (r) => json(r, { enabled: false, recipe_slug: null }));
    }
    const page = await ctx.newPage();
    await page.goto(`${BASE}/runtimes`);
    await settle(page, 2500);
    await shot(page, "mc-runtimes.png");
    await ctx.close();
  },

  /** Insights: tokens per day, where usage comes from, local share per week. */
  async "mc-insights"(browser, auth) {
    const ctx = await newContext(browser, auth, DESKTOP);
    await ctx.route("**/api/v1/intelligence/costs/by-day*", (r) => json(r, fx.usageByDay()));
    await ctx.route("**/api/v1/intelligence/costs/by-week*", (r) => json(r, fx.usageByWeek()));
    const page = await ctx.newPage();
    await page.goto(`${BASE}/insights`);
    await settle(page, 2500);
    await shot(page, "mc-insights.png");
    await ctx.close();
  },

  /** Settings → Autonomy: what agents may do alone, and what needs your tap. */
  async "mc-autonomy"(browser, auth) {
    const ctx = await newContext(browser, auth, DESKTOP);
    const page = await ctx.newPage();
    await page.goto(`${BASE}/settings`);
    await settle(page);
    await page.getByRole("button", { name: "Autonomy", exact: true }).first().click().catch(async () => {
      await page.getByText("Autonomy", { exact: true }).first().click();
    });
    await settle(page);
    await shot(page, "mc-autonomy.png");
    await ctx.close();
  },

  /** Home after the first start: the classic board view with the demo seed. */
  async "mc-home"(browser, auth) {
    const ctx = await newContext(browser, auth, DESKTOP);
    const snap = (i) => ({ ts: new Date(Date.now() - i * 60_000).toISOString(), cpu_pct: 12 + ((i * 37) % 11) + (i % 7 === 0 ? 9 : 0),
      memory_pct: 38 + (i % 5), memory_used_gb: 6.1, memory_total_gb: 16, disk_pct: 31, disk_used_gb: 79, disk_total_gb: 256,
      db_latency_ms: 0.6, redis_latency_ms: 0.3 });
    await ctx.route("**/api/v1/system/metrics/history*", (r) => {
      const snapshots = Array.from({ length: 60 }, (_, i) => snap(59 - i));
      json(r, { snapshots, count: snapshots.length });
    });
    // The demo stack runs without the worker container, so its watchdog and
    // host resources read "stopped" / 0 GB. Show a healthy install instead.
    await ctx.route("**/api/v1/system/status", async (r) => {
      const real = await (await r.fetch()).json();
      const up = { status: "running", source: "worker", last_seen: new Date().toISOString(), last_check: new Date().toISOString(), checks_total: 412 };
      json(r, { ...real, status: "healthy", components: { ...real.components, watchdog: up, task_runner: up },
        resources: snap(0) });
    });
    const page = await ctx.newPage();
    await page.goto(`${BASE}/`);
    await settle(page, 2000);
    await shot(page, "mc-home.png");
    await ctx.close();
  },

  /** First start: the wizard's last step (demo board + crew = the classic board). */
  async "mc-setup-wizard"(browser, auth) {
    const ctx = await newContext(browser, auth, DESKTOP);
    // As on a fresh install (the demo stack is already seeded at this point).
    await ctx.route("**/api/v1/boards", (r) => (r.request().method() === "GET" ? json(r, []) : r.fallback()));
    const page = await ctx.newPage();
    await page.goto(`${BASE}/setup`);
    await settle(page);
    await page.getByRole("button", { name: "Skip", exact: true }).first().click();
    await settle(page, 800);
    await page.getByRole("button", { name: /skip for now/i }).first().click();
    await settle(page, 800);
    await shot(page, "mc-setup-wizard.png");
    await ctx.close();
  },
};

const wanted = process.argv.slice(2);
fs.mkdirSync(OUT, { recursive: true });
const auth = await login();
const browser = await chromium.launch();
for (const [name, run] of Object.entries(SHOTS)) {
  if (wanted.length && !wanted.includes(name)) continue;
  console.log(name);
  await run(browser, auth);
}
await browser.close();

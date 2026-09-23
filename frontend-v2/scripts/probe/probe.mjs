#!/usr/bin/env node
// UI probe ("open everything"): visits every page at desktop and phone width,
// opens every panel, dropdown, menu, tab, accordion and dialog one after the
// other, screenshots each open state and reports optical problems.
//
// Write lock: every non-GET/HEAD/OPTIONS request is aborted and counted,
// WebSockets are refused, and controls with action labels are never clicked.
// (GET endpoints with server-side side effects remain — see docs/ui-probe.md.)
//
//   MC_PROBE_TOKEN=... npm run probe -- --base http://localhost --out /tmp/probe [--route /tasks] [--width 390]
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { clickVerdict, isCloser, redactSecrets } from "./lib/guard.mjs";
import { createLockedContext, lockSelfTest, newWriteCounter } from "./lib/context.mjs";
import { EXTRA_VIEWS, discoverRoutes, filterRoutes, resolveRoutes } from "./lib/routes.mjs";
import { LOADING_TEXT_RE, dedupeFindings, evaluateState } from "./lib/findings.mjs";
import { parseArgs, renderMarkdown, sampleRepeats, slug } from "./lib/report.mjs";
import { collectCandidates, markSeen, measureState, pageBusy, readSelect } from "./lib/browser.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP_DIR = resolve(HERE, "../../src/app");
const SETTLE_MS = 1500;
const AFTER_CLICK_MS = 600;
const NESTED_SEEN = "data-probe-seen2"; // second-level baseline, see markSeen()

const HELP = `Usage: npm run probe -- [--base URL] [--out DIR] [--route /path]... [--width N]... [--max-per-page N]
                        [--nested-max N] [--load-timeout MS] [--all-repeats] [--headed]
  Token: MC_PROBE_TOKEN (required, never printed). Defaults: --base http://localhost, widths 1440 and 390,
  --out <tmpdir>/mc-ui-probe/<timestamp>, --nested-max 20, --load-timeout 20000.
  --route takes an app pattern (/agents/[id], /tasks?task=[id]) or a prefix (/agents/* also takes /agents/[id]).`;

let opts;
try {
  opts = parseArgs(process.argv.slice(2));
} catch (e) {
  console.error(String(e.message || e));
  console.error(HELP);
  process.exit(64);
}
if (opts.help) {
  console.log(HELP);
  process.exit(0);
}
const TOKEN = process.env.MC_PROBE_TOKEN || "";
if (!TOKEN) {
  console.error("MC_PROBE_TOKEN is not set.");
  process.exit(64);
}

const OUT = resolve(opts.out);
mkdirSync(OUT, { recursive: true });
const startedAt = new Date().toISOString();

// ---- routes -------------------------------------------------------------
const getJson = async (endpoint) => {
  const res = await fetch(opts.base + endpoint, { method: "GET", headers: { Authorization: `Bearer ${TOKEN}` } });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
};
const wanted = filterRoutes([...discoverRoutes(APP_DIR), ...EXTRA_VIEWS], opts.routes);
if (!wanted.length) {
  console.error(`No route matches ${opts.routes.join(", ")}.`);
  process.exit(64);
}
const { resolved, skipped } = await resolveRoutes(wanted, getJson);
for (const s of skipped) console.log(`skip ${s.pattern}: ${s.reason}`);

// ---- write lock -----------------------------------------------------------
const writes = newWriteCounter();
const newContext = (browser, width) => createLockedContext(browser, width, TOKEN, writes, opts.base);
// Console text can carry URLs with ?token=… — scrub before it is stored.
const clean = (t) => redactSecrets(String(t), [TOKEN]).slice(0, 300);

// ---- one page at one width ----------------------------------------------------
async function probePage(ctx, route, width, shellDone) {
  const page = await ctx.newPage();
  const errors = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(clean(m.text()));
  });
  page.on("pageerror", (e) => errors.push(clean(`pageerror: ${String(e.message || e)}`)));
  page.on("dialog", (d) => d.dismiss().catch(() => {}));
  const takeErrors = () => errors.splice(0, errors.length);

  const dir = join(OUT, String(width), slug(route.path === "/" ? "home" : route.path, 60));
  mkdirSync(dir, { recursive: true });
  const rel = (f) => relative(OUT, f);
  const url = opts.base + route.path;
  // Live pages keep SSE/polling open, so "networkidle" may never come:
  // wait for the DOM, then give the network a short, capped quiet period.
  // Clicks may persist UI preferences (grouping, filters, open sections) in
  // browser storage. The snapshot taken after the first load is written back
  // before every reload, so each candidate starts from the same page state.
  let storageSnapshot = null;
  const load = async () => {
    if (storageSnapshot) {
      await page
        .evaluate((snap) => {
          for (const [store, data] of [[localStorage, snap.local], [sessionStorage, snap.session]]) {
            store.clear();
            for (const [k, v] of Object.entries(data)) store.setItem(k, v);
          }
        }, storageSnapshot)
        .catch(() => {});
    }
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => {});
    await page.waitForLoadState("networkidle", { timeout: 3000 }).catch(() => {});
    await page.waitForTimeout(SETTLE_MS);
    return waitReady();
  };
  // Slow endpoints leave "Loading…" on screen for seconds; probing then would
  // only see the loading state. Wait until no loading hint is visible. If the
  // first load never gets there, later reloads only wait briefly (the page is
  // reported as "still loading" once, not re-waited for every candidate).
  const budget = { load: opts.loadTimeoutMs, click: Math.min(opts.loadTimeoutMs, 10000) };
  const waitReady = async (which = "load") => {
    const until = Date.now() + budget[which];
    for (;;) {
      const hints = await page.evaluate(pageBusy, { pattern: LOADING_TEXT_RE.source, flags: LOADING_TEXT_RE.flags }).catch(() => []);
      if (!hints.length) return [];
      if (Date.now() >= until) {
        // something that never finishes loading: stop paying the full wait
        budget[which] = Math.max(1500, Math.floor(budget[which] / 2));
        return hints;
      }
      await page.waitForTimeout(500);
    }
  };

  const result = { path: route.path, pattern: route.pattern, width, states: [], findings: [], counts: {} };
  const push = (state, extra, isBase = false) => {
    const f = evaluateState(state, { isBase }).map((x) => ({ ...x, page: route.path, width, opener: extra.label || null, shot: extra.shot || null }));
    result.findings.push(...f);
    return f.length;
  };

  const stillLoading = await load();
  if (stillLoading.length) result.counts.loading = true;
  storageSnapshot = await page
    .evaluate(() => ({ local: { ...localStorage }, session: { ...sessionStorage } }))
    .catch(() => null);
  const baseShot = join(dir, "00-page.png");
  await page.screenshot({ path: baseShot, fullPage: true }).catch(() => {});
  const base = await page.evaluate(measureState, { base: true });
  if (base.url.split("?")[0] !== route.path.split("?")[0]) {
    result.redirectedTo = base.url;
  }
  push({ ...base, stillLoading, consoleErrors: takeErrors() }, { shot: rel(baseShot) }, true);

  let cands = await page.evaluate(collectCandidates, {});
  // Shell controls (sidebar, header) are probed on the first page per width only.
  const own = cands.filter((c) => c.scope === "page" || !shellDone.has(c.key));
  cands.filter((c) => c.scope === "shell").forEach((c) => shellDone.add(c.key));
  // Repeated components (one menu per row): first and last only.
  const { keep, skipped: sampledOut } = opts.allRepeats ? { keep: own, skipped: [] } : sampleRepeats(own);
  for (const c of sampledOut) result.states.push({ label: c.label, tag: c.tag, role: c.role, scope: c.scope, status: "sampled-out" });
  const list = opts.maxPerPage > 0 ? keep.slice(0, opts.maxPerPage) : keep;
  result.counts.candidates = list.length;
  result.counts.withoutAriaRole = list.filter((c) => !c.hasAriaRole).length;

  // Find a candidate again after a reload (indexes shift; key + ordinal do not).
  const locate = async (c) => {
    let sel = `[data-probe-c="${c.i}"]`;
    const ok = await page.evaluate(({ sel, key }) => {
      const e = document.querySelector(sel);
      if (!e) return false;
      const r = e.getBoundingClientRect();
      const label = (e.getAttribute("aria-label") || e.innerText || e.getAttribute("title") || e.getAttribute("placeholder") || e.getAttribute("name") || "").trim().replace(/\s+/g, " ").slice(0, 60);
      return r.width > 0 && r.height > 0 && `${e.tagName.toLowerCase()}|${e.getAttribute("role") || ""}|${label}` === key;
    }, { sel, key: c.key });
    if (ok) return sel;
    await load();
    const fresh = await page.evaluate(collectCandidates, {});
    const hit = fresh.find((x) => x.key === c.key && x.ordinal === c.ordinal);
    return hit ? `[data-probe-c="${hit.i}"]` : null;
  };

  // Open the parent candidate again from a clean load; returns its selector.
  const reopen = async (c) => {
    await load();
    const s2 = await locate(c);
    if (!s2) return null;
    await page.evaluate(markSeen);
    await page.click(s2, { timeout: 3000 }).catch(() => {});
    await page.waitForTimeout(AFTER_CLICK_MS);
    return s2;
  };
  const probeNested = async (c, sel, n, dir, rel) => {
    const out = { opened: 0, read: 0, failed: 0, reopened: 0, states: [] };
    if (opts.nestedMax === 0) return out;
    const gather = async () => {
      const all = await page.evaluate(collectCandidates, { onlyNew: true, attr: "data-probe-n" });
      return all.filter((x) => x.selected !== "true" && clickVerdict(x).ok && !isCloser(x.label));
    };
    let inner = await gather();
    const { keep } = opts.allRepeats ? { keep: inner } : sampleRepeats(inner);
    const todo = keep.slice(0, opts.nestedMax).map((x) => ({ key: x.key, ordinal: x.ordinal, label: x.label, tag: x.tag, role: x.role }));
    for (const x of todo) {
      let hit = inner.find((y) => y.key === x.key && y.ordinal === x.ordinal);
      const present = hit && (await page.evaluate((s3) => {
        const e = document.querySelector(s3);
        if (!e) return false;
        const r = e.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      }, `[data-probe-n="${hit.i}"]`));
      if (!present) {
        if (out.reopened >= 3) break;
        out.reopened += 1;
        if (!(await reopen(c))) break;
        inner = await gather();
        hit = inner.find((y) => y.key === x.key && y.ordinal === x.ordinal);
        if (!hit) {
          out.failed += 1;
          out.states.push({ label: x.label, status: "failed", reason: "not found after re-opening the parent" });
          continue;
        }
      }
      const nsel = `[data-probe-n="${hit.i}"]`;
      const label = `${c.label} › ${x.label || x.tag}`;
      if (x.tag === "select") {
        const info = await page.evaluate(readSelect, nsel);
        out.read += 1;
        out.states.push({ label: x.label, status: "read", options: info ? info.options : [] });
        continue;
      }
      takeErrors();
      await page.evaluate(markSeen, NESTED_SEEN);
      const before = page.url();
      try {
        await page.click(nsel, { timeout: 2000 });
      } catch (e) {
        out.failed += 1;
        out.states.push({ label: x.label, status: "failed", reason: String(e.message || e).split("\n")[0].slice(0, 120) });
        continue;
      }
      await page.waitForTimeout(400);
      const nestedLoading = await waitReady("click");
      if (new URL(page.url()).pathname !== new URL(before).pathname) {
        out.states.push({ label: x.label, status: "navigated", to: new URL(page.url()).pathname });
        out.reopened += 1;
        if (out.reopened > 3 || !(await reopen(c))) break;
        inner = await gather();
        continue;
      }
      const nst = await page.evaluate(measureState, { opener: nsel, seenAttr: NESTED_SEEN });
      const changed = nst.layers.length || (x.role === "tab" && nst.openerSelected === "true") || nst.openerExpanded === "true";
      if (!changed) {
        out.states.push({ label: x.label, status: "no-change" });
        takeErrors();
        continue;
      }
      out.opened += 1;
      const nshot = join(dir, `${String(n).padStart(3, "0")}-${slug(c.label || c.tag, 30)}--${String(out.opened).padStart(2, "0")}-${slug(x.label || x.tag, 24)}.png`);
      await page.screenshot({ path: nshot }).catch(() => {});
      push({ ...nst, stillLoading: nestedLoading, consoleErrors: takeErrors() }, { label, shot: rel(nshot) });
      out.states.push({ label: x.label, status: "opened", shot: rel(nshot) });
      // close a nested floating layer again (Escape closes the topmost only)
      if (nst.layers.some((l) => l.kind !== "inline")) {
        await page.keyboard.press("Escape").catch(() => {});
        await page.waitForTimeout(300);
      }
    }
    return out;
  };

  let n = 0;
  for (const c of list) {
    n += 1;
    const entry = { label: c.label, tag: c.tag, role: c.role, scope: c.scope };
    const verdict = clickVerdict(c);
    if (!verdict.ok) {
      result.states.push({ ...entry, status: verdict.reason === "disabled" ? "disabled" : "guarded", reason: verdict.reason });
      continue;
    }
    if (c.role === "tab" && c.selected === "true") {
      result.states.push({ ...entry, status: "already-open" });
      continue;
    }
    const sel = await locate(c);
    if (!sel) {
      result.states.push({ ...entry, status: "failed", reason: "not found after reload" });
      continue;
    }
    if (c.tag === "select") {
      const info = await page.evaluate(readSelect, sel);
      result.states.push({ ...entry, status: "read", native: true, options: info ? info.options : [] });
      continue;
    }
    takeErrors();
    await page.evaluate(markSeen);
    const before = page.url();
    try {
      await page.click(sel, { timeout: 3000 });
    } catch (e) {
      result.states.push({ ...entry, status: "failed", reason: String(e.message || e).split("\n")[0].slice(0, 120) });
      continue;
    }
    await page.waitForTimeout(AFTER_CLICK_MS);
    if (new URL(page.url()).pathname !== new URL(before).pathname) {
      result.states.push({ ...entry, status: "navigated", to: new URL(page.url()).pathname });
      await load();
      continue;
    }
    const clickLoading = await waitReady("click");
    const st = { ...(await page.evaluate(measureState, { opener: sel })), stillLoading: clickLoading };
    const toggled = c.expanded === "false" && st.openerExpanded === "true";
    const tabbed = c.role === "tab" && st.openerSelected === "true";
    if (!st.layers.length && !toggled && !tabbed && page.url() === before) {
      result.states.push({ ...entry, status: "no-change" });
      takeErrors();
      continue;
    }
    const shot = join(dir, `${String(n).padStart(3, "0")}-${slug(c.label || c.tag)}.png`);
    await page.screenshot({ path: shot }).catch(() => {});

    // A tab switch or a toggled section is not a layer that Escape should
    // close, even if the new view contains positioned elements.
    const floating = tabbed || toggled ? null : st.layers.find((l) => l.kind !== "inline");
    const floatingLeft = async () => (await page.evaluate(measureState, {})).layers.filter((l) => l.kind !== "inline").length;
    const nestedTargets = async () =>
      (await page.evaluate(collectCandidates, { onlyNew: true, attr: "data-probe-n" })).filter((x) => x.selected !== "true" && clickVerdict(x).ok && !isCloser(x.label)).length;
    const hasInner = opts.nestedMax > 0 && (await nestedTargets()) > 0;

    // 1) Escape check for floating layers FIRST, on the untouched layer — so a
    //    second-level layer left open can never be blamed on the parent.
    let escClosed = null;
    let closedHow = "escape";
    let dirty = false;
    if (floating) {
      await page.keyboard.press("Escape").catch(() => {});
      // give exit animations up to ~1.5 s before calling it "does not close"
      for (let t = 0; t < 6; t++) {
        await page.waitForTimeout(250);
        escClosed = (await floatingLeft()) === 0;
        if (escClosed) break;
      }
      if (!escClosed) {
        // Second opinion: a synthetic Escape on window. If only this one closes
        // the layer, the handler exists but a real key press does not reach it.
        await page.evaluate(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
        await page.waitForTimeout(500);
        if ((await floatingLeft()) === 0) {
          escClosed = "synthetic-only";
          closedHow = "synthetic-escape";
        }
      }
      if (escClosed === false) {
        // Tap the scrim in a corner — but only if what sits there belongs to
        // the new layer (never an unchecked page control).
        closedHow = "backdrop";
        const onScrim = await page.evaluate(() => {
          const t = document.elementFromPoint(3, 3);
          return !!t && !t.hasAttribute("data-probe-seen");
        });
        if (onScrim) {
          await page.mouse.click(3, 3).catch(() => {});
          await page.waitForTimeout(500);
        }
        dirty = (await floatingLeft()) > 0;
      }
    }

    // 2) Second level: every opener the first click revealed (dropdowns inside
    //    a settings tab, accordions in a section, tabs in a dialog, wizard
    //    steps, rows in a detail panel). A floating parent is opened again for
    //    this. If a nested click tears the parent down, it is re-opened from a
    //    fresh load and the pass continues.
    let nestedStats = { opened: 0, read: 0, failed: 0, reopened: 0, states: [] };
    if (hasInner) {
      let parentOpen = true;
      if (floating) {
        if (dirty) {
          await load();
          dirty = false;
        }
        const again = (await locate(c)) || null;
        if (again) {
          await page.evaluate(markSeen);
          await page.click(again, { timeout: 3000 }).catch(() => {});
          await page.waitForTimeout(AFTER_CLICK_MS);
          parentOpen = (await floatingLeft()) > 0;
        } else parentOpen = false;
      }
      if (parentOpen) nestedStats = await probeNested(c, sel, n, dir, rel);
      if (floating) {
        await page.keyboard.press("Escape").catch(() => {});
        await page.waitForTimeout(400);
        if ((await floatingLeft()) > 0) dirty = true;
      }
      if (nestedStats.reopened) dirty = true;
    }
    const nestedOpened = nestedStats.opened;

    // 3) Close inline states: click a toggle again, switch a tab back, reload
    //    after shell changes. Live data re-renders leave "new" inline nodes
    //    behind, so only the opener's own state decides.
    if (floating) {
      // handled above
    } else if (toggled) {
      const exp = await page.evaluate((s) => document.querySelector(s)?.getAttribute("aria-expanded") ?? null, sel);
      if (exp === "true") {
        closedHow = "click";
        await page.click(sel, { timeout: 2000 }).catch(() => {});
        await page.waitForTimeout(350);
      }
    } else if (c.role === "tab" && c.restoreKey) {
      // switch back to the tab that was active when the page loaded
      closedHow = "tab-restore";
      const back = await page.evaluate(collectCandidates, {});
      const hit = back.find((x) => x.key === c.restoreKey);
      if (hit) await page.click(`[data-probe-c="${hit.i}"]`, { timeout: 2000 }).catch(() => {});
      await page.waitForTimeout(300);
    } else if (c.scope === "shell") {
      // e.g. "Collapse sidebar": leaving it would change the layout under
      // every later candidate of this page.
      closedHow = "reload";
      dirty = true;
    } else {
      closedHow = "left-open"; // plain inline panels: next locate() reloads if needed
    }
    if (dirty || new URL(page.url()).search !== new URL(before).search) {
      closedHow = "reload";
      await load();
    }
    push({ ...st, escClosed, consoleErrors: takeErrors() }, { label: c.label, shot: rel(shot) });
    result.states.push({ ...entry, status: "opened", shot: rel(shot), layers: st.layers.map((l) => ({ kind: l.kind, role: l.role, rect: l.rect })), nestedOpened, nestedRead: nestedStats.read, nestedFailed: nestedStats.failed, nested: nestedStats.states, escClosed, closedHow });
  }

  const count = (s) => result.states.filter((x) => x.status === s).length;
  Object.assign(result.counts, {
    opened: count("opened"),
    read: count("read"),
    sampledOut: count("sampled-out"),
    guarded: count("guarded"),
    disabled: count("disabled"),
    alreadyOpen: count("already-open"),
    noChange: count("no-change"),
    navigated: count("navigated"),
    failed: count("failed"),
    nestedOpened: result.states.reduce((a, s) => a + (s.nestedOpened || 0), 0),
  });
  result.findings = dedupeFindings(result.findings);
  // hand the next page the same storage this one started with
  if (storageSnapshot) {
    await page
      .evaluate((snap) => {
        for (const [store, data] of [[localStorage, snap.local], [sessionStorage, snap.session]]) {
          store.clear();
          for (const [k, v] of Object.entries(data)) store.setItem(k, v);
        }
      }, storageSnapshot)
      .catch(() => {});
  }
  await page.close();
  return result;
}

// ---- run --------------------------------------------------------------------
const browser = await chromium.launch({ headless: !opts.headed });
const pages = [];
try {
  // Prove the lock on this very setup before touching any page.
  const st = await lockSelfTest(browser, opts.base, TOKEN);
  if (!st.ok) {
    console.error(`write-lock self-test FAILED: ${st.reason} — aborting, nothing was clicked.`);
    await browser.close();
    process.exit(3);
  }
  console.log(`write-lock self-test ok (probe POST blocked, ${st.passed} passed)`);

  for (const width of opts.widths) {
    const ctx = await newContext(browser, width);
    const shellDone = new Set();
    for (const route of resolved) {
      const t0 = Date.now();
      const r = await probePage(ctx, route, width, shellDone);
      pages.push(r);
      console.log(`${route.path} @${width}: opened ${r.counts.opened}/${r.counts.candidates}, guarded ${r.counts.guarded}, findings ${r.findings.length} (${Math.round((Date.now() - t0) / 1000)}s)`);
    }
    await ctx.close();
  }
} finally {
  await browser.close();
}

const result = {
  base: opts.base,
  startedAt,
  finishedAt: new Date().toISOString(),
  widths: opts.widths,
  skippedRoutes: skipped,
  lockSelfTest: "ok",
  writes,
  pages,
};
writeFileSync(join(OUT, "probe.json"), JSON.stringify(result, null, 1));
writeFileSync(join(OUT, "report.md"), renderMarkdown(result));
console.log(`writes: blocked ${writes.blocked}, passed ${writes.passed}, websockets refused ${writes.websocketsRefused}`);
console.log(`report: ${join(OUT, "report.md")}`);
// A write that got through is the one outcome this tool must never hide.
process.exit(writes.passed > 0 ? 2 : 0);

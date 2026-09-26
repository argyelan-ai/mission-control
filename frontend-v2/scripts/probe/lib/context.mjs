// Browser context with the hard write lock. Used by probe.mjs.
import { isWriteMethod, redactUrl } from "./guard.mjs";

export const newWriteCounter = () => ({ blocked: 0, passed: 0, websocketsRefused: 0, samples: [] });

/**
 * A fresh context in which no write can leave the browser:
 *  - every request that is not GET/HEAD/OPTIONS is aborted and counted,
 *  - every WebSocket is refused (a socket can carry writes page.route never sees),
 *  - service workers are blocked (they could bypass routing),
 *  - an independent listener counts writes that *finished* — must stay 0.
 */
export async function createLockedContext(browser, width, token, writes, base, extra = {}) {
  const mobile = width <= 500;
  const theme = extra.theme || null;
  const ctx = await browser.newContext({
    // the emulated OS preference matches a forced theme ("system" follows it)
    ...(theme === "light" || theme === "dark" ? { colorScheme: theme } : {}),
    viewport: { width, height: mobile ? 844 : 900 },
    isMobile: mobile,
    hasTouch: mobile,
    deviceScaleFactor: 1,
    serviceWorkers: "block",
    acceptDownloads: false,
  });
  await ctx.route("**/*", (route) => {
    const req = route.request();
    if (isWriteMethod(req.method())) {
      writes.blocked += 1;
      if (writes.samples.length < 40) writes.samples.push(`${req.method()} ${redactUrl(req.url())}`);
      return route.abort("blockedbyclient");
    }
    return route.continue();
  });
  await ctx.routeWebSocket(/.*/, (ws) => {
    writes.websocketsRefused += 1;
    if (writes.refusedSelfTest !== undefined && /__ui_probe_lock_selftest__/.test(ws.url())) writes.refusedSelfTest += 1;
    ws.close({ code: 1008, reason: "ui-probe: read-only" });
  });
  // Deliberately NOT isWriteMethod(): the second counter must not share code
  // with the lock it is checking.
  ctx.on("requestfinished", (req) => {
    if (!["GET", "HEAD", "OPTIONS"].includes(req.method().toUpperCase())) {
      writes.passed += 1;
      writes.samples.push(`PASSED(!) ${req.method()} ${redactUrl(req.url())}`);
    }
  });
  ctx.on("page", (p) => {
    // popups / new tabs opened by a click: close them right away
    p.opener().then((o) => (o ? p.close() : null)).catch(() => {});
  });
  if (token) {
    if (!base) throw new Error("createLockedContext: base URL required when a token is given");
    await ctx.addInitScript(tokenInitScript, { token, origin: new URL(base).origin });
  }
  if (theme) {
    if (!base) throw new Error("createLockedContext: base URL required when a theme is given");
    // Runs before the app's own <head> script (THEME_INIT_SCRIPT), which then
    // applies the stored choice before the first paint — the real code path.
    await ctx.addInitScript(themeInitScript, { key: THEME_STORAGE_KEY, theme, origin: new URL(base).origin });
  }
  return ctx;
}

/**
 * Runs in every frame before the page's own scripts. The token goes into
 * local storage only on the app's own origin — never into a foreign iframe or
 * popup, whose scripts could read it. `_loc` / `_ls` exist for unit tests.
 */
export function tokenInitScript(arg) {
  try {
    const loc = arg._loc || location;
    if (loc.origin !== arg.origin) return;
    (arg._ls || localStorage).setItem("mc_auth_token", arg.token);
  } catch {}
}

/** Must equal THEME_STORAGE_KEY in src/lib/themeScript.ts (a test checks it). */
export const THEME_STORAGE_KEY = "mc_theme";

/**
 * Init script for --theme: stores the theme choice in local storage on the
 * app's own origin before any page script runs. `_loc` / `_ls` for tests.
 */
export function themeInitScript(arg) {
  try {
    const loc = arg._loc || location;
    if (loc.origin !== arg.origin) return;
    (arg._ls || localStorage).setItem(arg.key, arg.theme);
  } catch {}
}

/**
 * Self-test run before every probe: a page in a locked context POSTs to a
 * path that does not exist and opens a WebSocket. The lock must block the
 * POST (blocked = 1, passed = 0) and refuse the socket (refused = 1, close code
 * 1008 from the probe, never from the server). Even if the lock were broken
 * both would only hit a path that does not exist — no data.
 */
export async function lockSelfTest(browser, base) {
  // Own counter for the self-test socket: a dev server's hot-reload socket
  // (next dev) is refused as well and must not fail the self-test.
  const w = { ...newWriteCounter(), refusedSelfTest: 0 };
  const ctx = await createLockedContext(browser, 1024, null, w);
  try {
    const page = await ctx.newPage();
    await page.goto(base + "/robots.txt", { waitUntil: "domcontentloaded", timeout: 15000 }).catch(() => {});
    const outcome = await page.evaluate(async (u) => {
      try {
        const r = await fetch(u, { method: "POST", body: "{}" });
        return `status ${r.status}`;
      } catch {
        return "rejected";
      }
    }, base + "/api/v1/__ui_probe_lock_selftest__");
    const wsOutcome = await page.evaluate(
      (u) =>
        new Promise((done) => {
          let ws;
          try {
            ws = new WebSocket(u);
          } catch {
            return done("throw");
          }
          const t = setTimeout(() => done("timeout"), 5000);
          ws.addEventListener("close", (e) => {
            clearTimeout(t);
            done(`closed:${e.code}`);
          });
        }),
      base.replace(/^http/, "ws") + "/api/v1/__ui_probe_lock_selftest__/ws",
    );
    await page.waitForTimeout(300);
    const ok = w.blocked === 1 && w.passed === 0 && outcome === "rejected" && w.refusedSelfTest === 1 && wsOutcome === "closed:1008";
    return {
      ok,
      passed: w.passed,
      reason: ok ? "" : `blocked=${w.blocked} passed=${w.passed} fetch=${outcome} wsRefused=${w.refusedSelfTest}/${w.websocketsRefused} ws=${wsOutcome}`,
    };
  } finally {
    await ctx.close();
  }
}

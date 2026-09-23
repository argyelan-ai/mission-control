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
export async function createLockedContext(browser, width, token, writes) {
  const mobile = width <= 500;
  const ctx = await browser.newContext({
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
    await ctx.addInitScript((t) => {
      try {
        localStorage.setItem("mc_auth_token", t);
      } catch {}
    }, token);
  }
  return ctx;
}

/**
 * Self-test run before every probe: a page in a locked context POSTs to a
 * path that does not exist. The lock must block it (blocked = 1, passed = 0).
 * Even if the lock were broken the request would only hit a 404 — no data.
 */
export async function lockSelfTest(browser, base) {
  const w = newWriteCounter();
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
    await page.waitForTimeout(300);
    const ok = w.blocked === 1 && w.passed === 0 && outcome === "rejected";
    return { ok, passed: w.passed, reason: ok ? "" : `blocked=${w.blocked} passed=${w.passed} fetch=${outcome}` };
  } finally {
    await ctx.close();
  }
}

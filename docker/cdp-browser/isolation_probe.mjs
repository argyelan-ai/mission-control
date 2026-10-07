// Real-Chromium probe for browser-session isolation (ADR-088 isolation step).
// Run by test_isolation.sh inside the playwright-mcp image (Node 22: global
// WebSocket + fetch; playwright-core from @playwright/mcp).
//
//   alpha  session A, a raw CDP client in Puppeteer's pattern (omp is
//          Puppeteer): setDiscoverTargets + setAutoAttach{autoAttach,
//          waitForDebuggerOnStart, flatten}, createTarget WITHOUT a context,
//          resumes what it is attached to.
//   beta   session B, real Playwright connectOverCDP: newContext() like
//          playwright-mcp --isolated, plus the default context.
//
// Usage: node isolation_probe.mjs <gateway-url> <expect: isolated|open>
// Prints one line per check, exits 1 if any check fails.
import { randomUUID } from "node:crypto";
import { createRequire } from "node:module";

const GW = (process.argv[2] || "http://cdp-browser:9300").replace(/\/$/, "");
const EXPECT = process.argv[3] || "isolated";
const PW_PATH = process.env.PLAYWRIGHT_CORE || "/usr/local/lib/node_modules/@playwright/mcp/node_modules/playwright-core";
const { chromium } = createRequire(import.meta.url)(PW_PATH);

let failed = 0;
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
  if (!ok) failed += 1;
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const within = (promise, ms) => Promise.race([promise, sleep(ms).then(() => "TIMEOUT")]);

// Low-entropy placeholders: test tokens, not credentials.
const session = (name) => ({ id: randomUUID(), token: `probe-${name}-`.padEnd(40, name[0]) });
const A = session("alpha");
const B = session("beta");

async function register(s) {
  const r = await fetch(`${GW}/mc/sessions/${s.token}?session=${s.id}`, { method: "PUT" });
  if (!r.ok) throw new Error(`register ${r.status}`);
}

async function targets() {
  return (await fetch(`${GW}/mc/targets`)).json();
}

class CdpClient {
  // Minimal flattened CDP client: commands, events, sessions.
  constructor(url) {
    this.ws = new WebSocket(url);
    this.nextId = 1;
    this.waiting = new Map();
    this.events = [];
    this.listeners = [];
    this.closed = new Promise((r) => this.ws.addEventListener("close", () => r("closed")));
    this.ws.addEventListener("message", (e) => {
      const msg = JSON.parse(e.data);
      if (msg.id && this.waiting.has(msg.id)) {
        this.waiting.get(msg.id)(msg);
        this.waiting.delete(msg.id);
        return;
      }
      this.events.push(msg);
      for (const fn of this.listeners) fn(msg);
    });
  }
  open() {
    return new Promise((resolve, reject) => {
      this.ws.addEventListener("open", resolve, { once: true });
      this.ws.addEventListener("error", reject, { once: true });
    });
  }
  send(method, params = {}, sessionId) {
    const id = this.nextId++;
    const msg = { id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    return new Promise((resolve) => {
      this.waiting.set(id, resolve);
      this.ws.send(JSON.stringify(msg));
    });
  }
  close() {
    this.ws.close();
  }
}

async function cdpFor(prefix) {
  const version = await (await fetch(`${GW}${prefix}/json/version`)).json();
  const client = new CdpClient(version.webSocketDebuggerUrl);
  await client.open();
  return client;
}

async function main() {
  await register(A);
  await register(B);

  // alpha: Puppeteer's connect sequence.
  const alpha = await cdpFor(`/s/${A.token}`);
  alpha.listeners.push((msg) => {
    if (msg.method === "Target.attachedToTarget" && msg.params.waitingForDebugger) {
      alpha.send("Runtime.runIfWaitingForDebugger", {}, msg.params.sessionId);
    }
  });
  await alpha.send("Target.setDiscoverTargets", { discover: true });
  await alpha.send("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: true, flatten: true });
  const created = await alpha.send("Target.createTarget", { url: "data:text/html,<title>ALPHA</title>" });
  const alphaTab = created.result?.targetId;
  check("alpha opens a tab with createTarget and no context", Boolean(alphaTab), JSON.stringify(created.error || ""));

  // beta: real Playwright, with alpha auto-attached + waitForDebuggerOnStart.
  const browser = await chromium.connectOverCDP(`${GW}/s/${B.token}/`);
  const ctx = await browser.newContext();
  const page = await within(ctx.newPage(), 15000);
  const loaded = page !== "TIMEOUT" && (await within(page.goto("data:text/html,<title>BETA</title>").then(() => page.title()), 15000));
  check("beta's new tab loads although alpha waits for the debugger on new tabs (hang regression)", loaded === "BETA", String(loaded));
  const defaultPage = await within(browser.contexts()[0].newPage(), 15000);
  const defaultLoaded = defaultPage !== "TIMEOUT" &&
    (await within(defaultPage.goto("data:text/html,<title>BETA-DEFAULT</title>").then(() => defaultPage.title()), 15000));
  check("beta's default-context tab loads too", defaultLoaded === "BETA-DEFAULT", String(defaultLoaded));

  await sleep(500);
  const rows = await targets();
  // Chromium reports a data: page's URL as its target title; match the marker in the URL.
  const tab = (list, marker) => list.find((r) => (r.url || "").includes(`<title>${marker}<`) || r.title === marker);
  const byTitle = (t) => tab(rows, t);
  check("alpha's tab belongs to session A", byTitle("ALPHA")?.session === A.id, JSON.stringify(byTitle("ALPHA")));
  check("beta's tab belongs to session B", byTitle("BETA")?.session === B.id, JSON.stringify(byTitle("BETA")));
  const betaIds = rows.filter((r) => r.session === B.id).map((r) => r.targetId);

  const alphaSaw = new Set(
    alpha.events
      .filter((m) => m.method?.startsWith("Target.") && (m.params?.targetInfo?.targetId || m.params?.targetId))
      .map((m) => m.params.targetInfo?.targetId || m.params.targetId),
  );
  const alphaList = (await alpha.send("Target.getTargets")).result.targetInfos.map((t) => t.targetId);
  const sawBeta = betaIds.some((id) => alphaSaw.has(id) || alphaList.includes(id));

  if (EXPECT === "open") {
    check("switch off: alpha sees beta's tabs exactly as before", sawBeta, `betaIds=${betaIds.length}`);
    alpha.close();
    await browser.close();
    return;
  }

  check("alpha never hears of beta's tabs (events, getTargets)", !sawBeta && betaIds.length >= 1,
    `alpha list=${alphaList.length}, betaIds=${betaIds.length}`);
  check("alpha's getTargets lists its own tab", alphaList.includes(alphaTab));

  const attach = await alpha.send("Target.attachToTarget", { targetId: betaIds[0], flatten: true });
  check("alpha cannot attach to beta's tab", Boolean(attach.error), JSON.stringify(attach.error || attach.result));
  const closeTry = await alpha.send("Target.closeTarget", { targetId: betaIds[0] });
  check("alpha cannot close beta's tab", Boolean(closeTry.error));

  // Cookie probe: set by alpha without a context, never visible to beta or
  // to the shared default context.
  await alpha.send("Storage.setCookies", { cookies: [{ name: "mc_probe", value: "1", domain: "probe.test", path: "/" }] });
  const alphaCookies = (await alpha.send("Storage.getCookies")).result?.cookies || [];
  check("alpha reads its own cookie back", alphaCookies.some((c) => c.name === "mc_probe"));
  const betaCookies = await ctx.cookies("http://probe.test/");
  const betaDefaultCookies = await browser.contexts()[0].cookies("http://probe.test/");
  check("beta does not get alpha's cookie", !betaCookies.some((c) => c.name === "mc_probe") &&
    !betaDefaultCookies.some((c) => c.name === "mc_probe"));
  const shared = await cdpFor("");
  const sharedCookies = (await shared.send("Storage.getCookies")).result?.cookies || [];
  check("the shared default context does not get alpha's cookie", !sharedCookies.some((c) => c.name === "mc_probe"));
  const sharedList = (await shared.send("Target.getTargets")).result.targetInfos.map((t) => t.targetId);
  check("an unprefixed client still sees every tab (not filtered)", sharedList.includes(alphaTab) && betaIds.every((id) => sharedList.includes(id)));
  shared.close();

  // Browser.close from alpha ends alpha's connection only.
  const closed = await alpha.send("Browser.close");
  check("Browser.close is answered with {}", JSON.stringify(closed.result) === "{}", JSON.stringify(closed));
  check("alpha's connection ends", (await within(alpha.closed, 3000)) === "closed");
  await sleep(500);
  const alive = await fetch(`${GW}/json/version`).then((r) => r.ok).catch(() => false);
  check("Chromium keeps running", alive);
  check("beta keeps working", (await within(page.evaluate(() => 1 + 1), 5000)) === 2);

  // Ending session A disposes its context: its tab is gone, beta's stay.
  const ended = await (await fetch(`${GW}/mc/sessions/${A.token}`, { method: "DELETE" })).json();
  await sleep(500);
  const after = await targets();
  check("ending session A closes its tab via its context", Boolean(byTitle("ALPHA")) && !tab(after, "ALPHA") && ended.disposedContexts >= 1,
    JSON.stringify(ended));
  check("beta's tabs survive", Boolean(tab(after, "BETA")));
  await browser.close();
  await fetch(`${GW}/mc/sessions/${B.token}`, { method: "DELETE" });
}

try {
  await main();
} catch (err) {
  check("probe ran to the end", false, err.stack || String(err));
}
console.log(failed ? `FAILED ${failed}` : "ALL PASSED");
process.exit(failed ? 1 : 0);

/**
 * Mobile screenshot validation script — iPhone 16 (390×844)
 * Usage: npx tsx scripts/mobile-screenshot.ts [mobile|desktop|both]
 *
 * Requires:
 *   - Next.js dev server running on http://localhost:3001
 *   - Valid MC credentials in MC_EMAIL / MC_PASSWORD env vars, or hardcoded below
 */

import { chromium, type Browser, type BrowserContext, type Page } from "playwright";
import { mkdir, writeFile } from "fs/promises";
import { existsSync } from "fs";

const BASE_URL = process.env.MC_URL ?? "http://localhost:3001";
const EMAIL    = process.env.MC_EMAIL    ?? "admin@mc.local";
const PASSWORD = process.env.MC_PASSWORD ?? "changeme";

const ROUTES: { path: string; label: string; waitMs?: number }[] = [
  { path: "/login",     label: "login",     waitMs: 500  },
  { path: "/",          label: "home",      waitMs: 1200 },
  { path: "/tasks",     label: "tasks",     waitMs: 1000 },
  { path: "/inbox",     label: "inbox",     waitMs: 800  },
  { path: "/agents",    label: "agents",    waitMs: 800  },
  { path: "/schedule",  label: "schedule",  waitMs: 800  },
  { path: "/memory",    label: "memory",    waitMs: 800  },
  { path: "/insights",  label: "insights",  waitMs: 800  },
  { path: "/chat",      label: "chat",      waitMs: 600  },
  { path: "/office",    label: "office",    waitMs: 1500 },
  { path: "/runtimes",  label: "runtimes",  waitMs: 800  },
  { path: "/workflows", label: "workflows", waitMs: 800  },
  { path: "/content",   label: "content",   waitMs: 800  },
  { path: "/sessions",  label: "sessions",  waitMs: 800  },
  { path: "/skills",    label: "skills",    waitMs: 800  },
  { path: "/settings",  label: "settings",  waitMs: 800  },
  { path: "/news",      label: "news",      waitMs: 800  },
];

const MOBILE_VIEWPORT  = { width: 390,  height: 844  };
const DESKTOP_VIEWPORT = { width: 1440, height: 900  };
const MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1";

async function login(page: Page): Promise<string | null> {
  try {
    await page.goto(`${BASE_URL}/login`, { waitUntil: "networkidle", timeout: 15000 });
    const emailInput = page.locator('input[type="email"], input[name="email"], input[placeholder*="mail" i]').first();
    const passInput  = page.locator('input[type="password"]').first();
    if (!(await emailInput.isVisible())) {
      console.log("  Login page not found — checking if already logged in");
      return null;
    }
    await emailInput.fill(EMAIL);
    await passInput.fill(PASSWORD);
    await page.keyboard.press("Enter");
    await page.waitForNavigation({ timeout: 8000 }).catch(() => {});
    // Extract token from localStorage
    const token = await page.evaluate(() => {
      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i)!;
        const val = localStorage.getItem(key)!;
        if (val && val.length > 20 && (key.includes("token") || key.includes("auth"))) return val;
      }
      return null;
    });
    return token;
  } catch (e) {
    console.warn("  Login failed:", e);
    return null;
  }
}

async function injectToken(page: Page, token: string) {
  await page.addInitScript((t) => {
    localStorage.setItem("mc_auth_token", t);
    localStorage.setItem("mc_user", JSON.stringify({ email: "admin@example.com", role: "admin" }));
  }, token);
}

async function screenshotRoute(
  ctx: BrowserContext,
  route: typeof ROUTES[number],
  dir: string,
  token: string | null,
): Promise<void> {
  const page = await ctx.newPage();
  try {
    if (token) await injectToken(page, token);
    await page.goto(`${BASE_URL}${route.path}`, { waitUntil: "domcontentloaded", timeout: 20000 });
    await page.waitForTimeout(route.waitMs ?? 800);
    const outPath = `${dir}/${route.label}.png`;
    await page.screenshot({ path: outPath, fullPage: true });
    console.log(`  ✓ ${route.label} → ${outPath}`);
  } catch (e) {
    console.error(`  ✗ ${route.label}: ${e}`);
  } finally {
    await page.close();
  }
}

async function runViewport(
  browser: Browser,
  label: string,
  viewport: { width: number; height: number },
  userAgent: string | undefined,
  token: string | null,
) {
  const dir = `.screenshots/${label}`;
  await mkdir(dir, { recursive: true });
  console.log(`\n📱 Screenshotting ${label} (${viewport.width}×${viewport.height})...`);

  const ctx = await browser.newContext({
    viewport,
    deviceScaleFactor: label === "mobile" ? 2 : 1,
    isMobile: label === "mobile",
    hasTouch: label === "mobile",
    ...(userAgent ? { userAgent } : {}),
  });

  for (const route of ROUTES) {
    await screenshotRoute(ctx, route, dir, token);
  }

  await ctx.close();
}

async function main() {
  const mode = (process.argv[2] ?? "both") as "mobile" | "desktop" | "both";

  // Ensure screenshot dirs exist
  await mkdir(".screenshots/mobile",  { recursive: true });
  await mkdir(".screenshots/desktop", { recursive: true });

  const browser = await chromium.launch({ headless: true });

  // Use pre-generated token if provided, otherwise try UI login
  let token: string | null = process.env.MC_TOKEN ?? null;
  if (token) {
    console.log(`🔐 Using MC_TOKEN (${token.substring(0, 12)}...)`);
  } else {
    console.log("🔐 Logging in via UI...");
    const loginCtx = await browser.newContext({ viewport: DESKTOP_VIEWPORT });
    const loginPage = await loginCtx.newPage();
    token = await login(loginPage);
    await loginPage.close();
    await loginCtx.close();
    console.log(token ? `  Token obtained (${token.substring(0, 12)}...)` : "  No token — pages may redirect to login");
  }

  if (mode === "mobile" || mode === "both") {
    await runViewport(browser, "mobile",  MOBILE_VIEWPORT,  MOBILE_UA, token);
  }
  if (mode === "desktop" || mode === "both") {
    await runViewport(browser, "desktop", DESKTOP_VIEWPORT, undefined,  token);
  }

  await browser.close();
  console.log("\n✅ Done. Screenshots in .screenshots/");
}

main().catch((e) => { console.error(e); process.exit(1); });

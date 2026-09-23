// Route discovery for the UI probe — derives the page list from the Next.js
// app directory so a new page is probed without anyone editing a list.
import { readdirSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";

/**
 * Dynamic routes and where to find a real id for them. The probe GETs the
 * endpoint and takes the first item's id. Anything dynamic that is not listed
 * here is skipped and reported — never guessed.
 */
export const DYNAMIC_SOURCES = {
  "/agents/[id]": { endpoint: "/api/v1/agents", idField: "id" },
  "/schedule/[jobId]": { endpoint: "/api/v1/schedule/jobs", idField: "id" },
  // Task detail is a panel on /tasks, opened by a card without a button role.
  // Tasks live per board: first board, then its first task.
  "/tasks?task=[id]": { chain: [{ endpoint: "/api/v1/boards" }, { endpoint: "/api/v1/boards/{id}/tasks" }] },
};

/**
 * Views that are not their own page.tsx but a panel reached by a deep link
 * (the big ones a probe must not miss). Resolved like dynamic routes.
 */
export const EXTRA_VIEWS = [{ pattern: "/tasks?task=[id]", dynamic: true }];

/** Convert a page.tsx path (relative to the app dir) into a route pattern. */
export function pageFileToRoute(relPath) {
  const parts = relPath.split(/[\\/]/).slice(0, -1); // drop "page.tsx"
  const segs = [];
  for (const p of parts) {
    if (p.startsWith("_")) return null; // private folder: not routable
    if (p.startsWith("@")) continue; // parallel-route slot
    if (/^\(.*\)$/.test(p)) continue; // route group
    segs.push(p);
  }
  return "/" + segs.join("/");
}

/** Recursively list page.tsx / page.jsx / page.ts / page.js files. */
export function findPageFiles(appDir) {
  const out = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      if (name === "node_modules" || name === "__tests__") continue;
      const full = join(dir, name);
      const st = statSync(full);
      if (st.isDirectory()) walk(full);
      else if (/^page\.(tsx|jsx|ts|js)$/.test(name)) out.push(relative(appDir, full).split(sep).join("/"));
    }
  };
  walk(appDir);
  return out.sort();
}

/** Route patterns in stable order, with dynamic segments flagged. */
export function discoverRoutes(appDir) {
  const seen = new Set();
  const routes = [];
  for (const f of findPageFiles(appDir)) {
    const pattern = pageFileToRoute(f);
    if (!pattern || seen.has(pattern)) continue;
    seen.add(pattern);
    routes.push({ pattern, dynamic: /\[[^\]]+\]/.test(pattern), file: f });
  }
  return routes;
}

/**
 * Filter routes by --route arguments. A filter matches its exact pattern, and a
 * static filter like "/agents" also matches "/agents/[id]" only when written as
 * "/agents/*".
 */
export function filterRoutes(routes, filters) {
  if (!filters || filters.length === 0) return routes;
  return routes.filter((r) =>
    filters.some((f) => {
      if (f.endsWith("/*")) {
        const base = f.slice(0, -2);
        return r.pattern === base || r.pattern.startsWith(base + "/") || r.pattern.startsWith(base + "?");
      }
      return r.pattern === f;
    }),
  );
}

/** Pick the first usable id from an API list response. */
export function firstId(payload, idField = "id") {
  const list = Array.isArray(payload)
    ? payload
    : Array.isArray(payload?.items)
      ? payload.items
      : Array.isArray(payload?.data)
        ? payload.data
        : [];
  for (const item of list) {
    const v = item?.[idField];
    if (typeof v === "string" && v) return v;
    if (typeof v === "number") return String(v);
  }
  return null;
}

/**
 * Resolve every route to a concrete path.
 * @param {Array<{pattern: string, dynamic: boolean}>} routes
 * @param {(endpoint: string) => Promise<unknown>} getJson  GET-only fetcher
 * @returns {Promise<{resolved: Array<{pattern, path}>, skipped: Array<{pattern, reason}>}>}
 */
export async function resolveRoutes(routes, getJson, sources = DYNAMIC_SOURCES) {
  const resolved = [];
  const skipped = [];
  for (const r of routes) {
    if (!r.dynamic) {
      resolved.push({ pattern: r.pattern, path: r.pattern });
      continue;
    }
    const src = sources[r.pattern];
    if (!src) {
      skipped.push({ pattern: r.pattern, reason: "no id source configured (DYNAMIC_SOURCES)" });
      continue;
    }
    // A source is one GET or a chain of GETs, each step's "{id}" filled with
    // the previous step's first id (board -> task).
    const steps = src.chain || [src];
    let id = null;
    let failed = null;
    for (const step of steps) {
      const endpoint = step.endpoint.replace("{id}", encodeURIComponent(id ?? ""));
      try {
        id = firstId(await getJson(endpoint), step.idField || "id");
      } catch (e) {
        failed = `GET ${endpoint} failed: ${String(e?.message || e).slice(0, 120)}`;
        break;
      }
      if (!id) {
        failed = `GET ${endpoint} returned no items`;
        break;
      }
    }
    if (failed) {
      skipped.push({ pattern: r.pattern, reason: failed });
      continue;
    }
    const path = r.pattern.replace(/\[[^\]]+\]/, encodeURIComponent(id));
    if (/\[[^\]]+\]/.test(path)) {
      skipped.push({ pattern: r.pattern, reason: "more than one dynamic segment" });
      continue;
    }
    resolved.push({ pattern: r.pattern, path });
  }
  return { resolved, skipped };
}

#!/usr/bin/env node
// Guard for the BUILT browser bundle: no hard-coded backend origin.
//
// The browser must talk to the API same-origin (relative /api/..., routed by
// Caddy). A `localhost:8000` inside the client bundle points every viewer on
// another device (laptop, phone) at their OWN machine — finding 17.10.2026,
// a fresh install opened from a laptop: all live streams failed. NEXT_PUBLIC_*
// values are frozen in at build time, so a published image with such a URL
// cannot be fixed by any setting; this check runs on the build output.
//
// Usage: node scripts/check-bundle-origins.mjs [static-dir]
//   static-dir defaults to .next/static (after `npm run build`).
// Exit 0 = clean, 1 = hit (file list printed), 2 = nothing to scan.
import { readdirSync, readFileSync, statSync, existsSync } from "node:fs";
import { join, relative } from "node:path";

const FORBIDDEN = /(?:localhost|127\.0\.0\.1):8000/;
const root = process.argv[2] ?? join(process.cwd(), ".next", "static");

if (!existsSync(root)) {
  console.error(`check-bundle-origins: ${root} does not exist — run \`npm run build\` first`);
  process.exit(2);
}

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (/\.(js|mjs|html|json)$/.test(name)) out.push(p);
  }
  return out;
}

const files = walk(root);
if (files.length === 0) {
  console.error(`check-bundle-origins: no bundle files under ${root}`);
  process.exit(2);
}
const hits = files.filter((f) => FORBIDDEN.test(readFileSync(f, "utf8")));
if (hits.length > 0) {
  console.error("✗ client bundle carries a hard-coded backend origin (localhost:8000):");
  for (const f of hits) console.error(`  ${relative(process.cwd(), f)}`);
  console.error("  The browser must use relative /api/... URLs (lib/authToken.ts BASE_URL).");
  process.exit(1);
}
console.log(`✓ ${files.length} client bundle files, no localhost:8000`);

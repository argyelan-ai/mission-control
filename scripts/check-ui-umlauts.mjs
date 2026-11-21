#!/usr/bin/env node
/**
 * check-ui-umlauts — no German words hard-coded in the web UI.
 *
 * UI text lives in frontend-v2/messages/<locale>.json (docs/i18n.md). A German
 * letter (ä ö ü Ä Ö Ü ß) in JSX text or in a `title` / `aria-label` /
 * `placeholder` / `alt` attribute of a .tsx file is a string that shows German
 * on an English screen. This guard finds them.
 *
 * Scope: frontend-v2/src/**\/*.tsx, without tests (__tests__/, *.test.tsx) and
 * without comments. messages/ is the place German belongs, so it is not read.
 *
 * Ratchet: findings that existed when the guard came in are listed in
 * scripts/check-ui-umlauts.baseline.json (file → count). A file may only go
 * DOWN. A new file or a higher count fails. Lower a file's number (or drop the
 * file) in the same PR that cleans it up — `--update` rewrites the baseline,
 * but only ever downwards.
 *
 * Usage:  node scripts/check-ui-umlauts.mjs            (exit 1 on a new finding)
 *         node scripts/check-ui-umlauts.mjs --list     (print every finding)
 *         node scripts/check-ui-umlauts.mjs --update   (shrink the baseline)
 * The frontend test suite runs the same check (src/__tests__/uiUmlautGuard.test.ts),
 * so CI enforces it through `npm run test:run`.
 */
import { readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
export const REPO = join(HERE, "..");
export const SRC = join(REPO, "frontend-v2", "src");
export const BASELINE = join(HERE, "check-ui-umlauts.baseline.json");

const GERMAN = /[äöüÄÖÜß]/;
const ATTR = /\b(?:title|aria-label|placeholder|alt)\s*=\s*(?:"([^"]*)"|'([^']*)'|\{\s*["'`]([^"'`]*)["'`]\s*\})/g;

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) return name === "__tests__" || name === "node_modules" ? [] : walk(p);
    return name.endsWith(".tsx") && !name.endsWith(".test.tsx") ? [p] : [];
  });
}

/** Blank out comments (keeping line breaks) so commented German never counts. */
export function stripComments(src) {
  let out = "";
  let i = 0;
  let quote = null;
  while (i < src.length) {
    const c = src[i];
    const n = src[i + 1];
    if (quote) {
      out += c;
      if (c === "\\") { out += n ?? ""; i += 2; continue; }
      if (c === quote) quote = null;
      i++;
      continue;
    }
    if (c === "/" && n === "*") {
      const end = src.indexOf("*/", i + 2);
      const stop = end === -1 ? src.length : end + 2;
      out += src.slice(i, stop).replace(/[^\n]/g, " ");
      i = stop;
      continue;
    }
    if (c === "/" && n === "/" && src[i - 1] !== ":") {
      const end = src.indexOf("\n", i);
      const stop = end === -1 ? src.length : end;
      out += " ".repeat(stop - i);
      i = stop;
      continue;
    }
    if (c === '"' || c === "'" || c === "`") quote = c;
    out += c;
    i++;
  }
  return out;
}

/** Findings in one source text: [{ line, kind, text }]. */
export function scanSource(src) {
  const code = stripComments(src);
  const lines = code.split("\n");
  const hits = [];
  lines.forEach((line, idx) => {
    if (!GERMAN.test(line)) return;
    // Attributes a person reads or hears.
    for (const m of line.matchAll(ATTR)) {
      const text = m[1] ?? m[2] ?? m[3] ?? "";
      if (GERMAN.test(text)) hits.push({ line: idx + 1, kind: "attr", text: text.trim() });
    }
    // JSX text: what sits between a tag's ">" and the next "<" / "{".
    for (const m of line.matchAll(/>([^<>{}]*)/g)) {
      const text = m[1];
      if (GERMAN.test(text) && !/=>/.test(line.slice(Math.max(0, m.index - 1), m.index + 1))) {
        hits.push({ line: idx + 1, kind: "jsx", text: text.trim() });
      }
    }
    // A line of bare JSX text (inside an element spanning several lines).
    const bare = line.trim();
    if (
      GERMAN.test(bare) &&
      /^[^<>{}()=;"'`]+$/.test(bare) &&
      !/[:,]$/.test(bare) &&
      idx > 0 && /[>}]\s*$/.test(lines[idx - 1].trim())
    ) {
      hits.push({ line: idx + 1, kind: "jsx", text: bare });
    }
  });
  return hits;
}

export function scanAll() {
  const out = {};
  for (const file of walk(SRC)) {
    const hits = scanSource(readFileSync(file, "utf-8"));
    if (hits.length) out[relative(REPO, file)] = hits;
  }
  return out;
}

export function readBaseline() {
  try {
    return JSON.parse(readFileSync(BASELINE, "utf-8"));
  } catch {
    return {};
  }
}

/** Files whose count went above the baseline (or are new): { file: {now, allowed, hits} }. */
export function regressions(findings = scanAll(), baseline = readBaseline()) {
  const bad = {};
  for (const [file, hits] of Object.entries(findings)) {
    const allowed = baseline[file] ?? 0;
    if (hits.length > allowed) bad[file] = { now: hits.length, allowed, hits };
  }
  return bad;
}

function main(argv) {
  const findings = scanAll();
  if (argv.includes("--list")) {
    for (const [file, hits] of Object.entries(findings)) {
      for (const h of hits) console.log(`${file}:${h.line}  [${h.kind}]  ${h.text}`);
    }
  }
  if (argv.includes("--update")) {
    const base = readBaseline();
    const next = {};
    for (const [file, hits] of Object.entries(findings)) {
      const prev = base[file];
      // Only ever down: a new or grown file is not written in.
      if (prev !== undefined) next[file] = Math.min(prev, hits.length);
    }
    writeFileSync(BASELINE, JSON.stringify(Object.fromEntries(Object.entries(next).sort()), null, 2) + "\n");
    console.log(`baseline: ${Object.keys(next).length} files`);
  }
  const bad = regressions(findings);
  const files = Object.keys(bad);
  if (files.length === 0) {
    console.log("check-ui-umlauts: OK — no new German text in .tsx outside messages/");
    return 0;
  }
  for (const f of files) {
    console.error(`${f}: ${bad[f].now} German UI string(s), allowed ${bad[f].allowed}`);
    for (const h of bad[f].hits) console.error(`  ${f}:${h.line}  [${h.kind}]  ${h.text}`);
  }
  console.error("Move the text into frontend-v2/messages/<locale>.json and use t() (docs/i18n.md).");
  return 1;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  process.exit(main(process.argv.slice(2)));
}

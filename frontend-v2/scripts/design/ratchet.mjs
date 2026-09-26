#!/usr/bin/env node
// Design-token ratchet (DESIGN.md "Komposition" K5/K6 and the scales in
// globals.css). Counts free values that bypass the tokens — arbitrary pixel
// font sizes, off-scale spacing, inline hex/rgba colours, free tracking — and
// compares the TOTAL per rule with ratchet-baseline.json:
//   more than the baseline  → red (a new free value slipped in),
//   less than the baseline  → red too, until the baseline is lowered with
//                             `npm run design:ratchet -- --update` in the same PR,
//                             so a won improvement cannot be used up again silently.
// Totals only: renaming or splitting a file does not change them.
//
// It does not catch everything (dynamic class names, values in .css files);
// "not worse" still works with gaps. The operator's look at the picture decides.
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const SPACE = "(?:p|px|py|pt|pb|pl|pr|ps|pe|m|mx|my|mt|mb|ml|mr|ms|me|gap|gap-x|gap-y|space-x|space-y|inset|top|left|right|bottom)";

/** Each rule: a regex over component source. Keep the ids stable — the baseline uses them. */
export const RULES = {
  // free font sizes: text-[13px], text-[0.8rem], text-[10.5px]
  "font-free": /\btext-\[\d+(?:\.\d+)?(?:px|rem|em)\]/g,
  // inline font sizes: fontSize: 13 / fontSize: "13px"
  "font-inline": /\bfontSize:\s*["'`]?\d/g,
  // spacing classes off the 4-px scale {0,4,8,12,16,24,32,48,64}: p-1.5, gap-5, mt-7, px-2.5 …
  "space-offscale": new RegExp(`(?<![\\w-])-?${SPACE}-(?:0\\.5|1\\.5|2\\.5|3\\.5|5|7|9|10|11|14)(?![\\w.])`, "g"),
  // arbitrary spacing: p-[6px], gap-[10px]
  "space-free": new RegExp(`(?<![\\w-])-?${SPACE}-\\[[^\\]]+\\]`, "g"),
  // inline spacing numbers: padding: 6, marginTop: "10px", gap: 14
  "space-inline": /\b(?:padding|margin|gap|rowGap|columnGap)(?:Top|Bottom|Left|Right|Inline|Block)?:\s*["'`]?\d/g,
  // colours outside colors.ts: #aabbcc, #aabbccdd, #abc with a letter (all-digit
  // "#632" is a PR number, so "#000" slips through — accepted), rgb()/rgba()/hsl()
  "color-literal": /#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|(?=[0-9]*[a-fA-F])[0-9a-fA-F]{3})(?![0-9a-zA-Z_-])|\b(?:rgba?|hsla?)\(/g,
  // free letter-spacing and line-height: tracking-[0.14em], leading-[1.3]
  "tracking-free": /\b(?:tracking|leading)-\[[^\]]+\]/g,
};

/** The token source of truth is allowed to hold literals. */
export const EXEMPT = [/^lib\/colors\.ts$/, /^components\/homepage\/colors\.ts$/];

export function countRules(src) {
  const out = {};
  for (const [id, re] of Object.entries(RULES)) out[id] = (src.match(re) || []).length;
  return out;
}

/**
 * Theme guard for globals.css: font weights must not live under --font-*
 * (Tailwind v4 reads --font-<name> as a font FAMILY, so `font-semibold`
 * silently rendered at 400 — see the open fix #326), and no font-weight may
 * read a --font-* variable.
 */
export function countThemeWeights(css) {
  const numericFontVars = (css.match(/--font-(?!weight-)[\w-]+:\s*\d{3}\s*;/g) || []).length;
  const weightFromFontVar = (css.match(/font-weight:\s*var\(--font-(?!weight-)[\w-]+\)/g) || []).length;
  return { "theme-font-weight": numericFontVars + weightFromFontVar };
}

const walk = (dir) =>
  readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    if (e.isDirectory()) return ["__tests__", "node_modules", "test", "tests"].includes(e.name) ? [] : walk(p);
    return /\.(tsx|ts)$/.test(e.name) && !/\.(test|spec)\.tsx?$/.test(e.name) && !e.name.endsWith(".d.ts") ? [p] : [];
  });

/** Totals per rule over src/ plus the theme guard; perFile only for the hint on failure. */
export function scan(srcDir) {
  const totals = Object.fromEntries([...Object.keys(RULES), "theme-font-weight"].map((k) => [k, 0]));
  const perFile = {};
  for (const f of walk(srcDir)) {
    const rel = relative(srcDir, f).split("\\").join("/");
    if (EXEMPT.some((re) => re.test(rel))) continue;
    const c = countRules(readFileSync(f, "utf8"));
    for (const [k, n] of Object.entries(c)) {
      totals[k] += n;
      if (n) (perFile[k] ??= {})[rel] = n;
    }
  }
  const css = readFileSync(join(srcDir, "styles/globals.css"), "utf8");
  totals["theme-font-weight"] = countThemeWeights(css)["theme-font-weight"];
  return { totals, perFile };
}

/** Compare totals with the baseline. Returns human-readable problems (empty = green). */
export function compare(totals, baseline) {
  const problems = [];
  for (const [rule, n] of Object.entries(totals)) {
    const allowed = baseline[rule];
    if (allowed === undefined) problems.push(`${rule}: no baseline yet (${n}) — run the update`);
    else if (n > allowed) problems.push(`${rule}: ${n} > baseline ${allowed} — ${n - allowed} new free value(s); use a token from DESIGN.md instead`);
    else if (n < allowed) problems.push(`${rule}: ${n} < baseline ${allowed} — well done; lock it in with \`npm run design:ratchet -- --update\``);
  }
  return problems;
}

const HERE = dirname(fileURLToPath(import.meta.url));
export const SRC_DIR = resolve(HERE, "../../src");
export const BASELINE = join(HERE, "ratchet-baseline.json");

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const { totals, perFile } = scan(SRC_DIR);
  if (process.argv.includes("--update")) {
    writeFileSync(BASELINE, JSON.stringify(totals, null, 2) + "\n");
    console.log("baseline updated:", JSON.stringify(totals));
    process.exit(0);
  }
  const problems = compare(totals, JSON.parse(readFileSync(BASELINE, "utf8")));
  console.log(JSON.stringify(totals));
  if (problems.length) {
    console.log(problems.join("\n"));
    if (process.argv.includes("--files")) console.log(JSON.stringify(perFile, null, 1));
    process.exit(1);
  }
  console.log("design ratchet: green");
}

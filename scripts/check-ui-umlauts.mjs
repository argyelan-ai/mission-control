#!/usr/bin/env node
/**
 * check-ui-umlauts — no German words hard-coded in the web UI.
 *
 * UI text lives in frontend-v2/messages/<locale>.json (docs/i18n.md). A German
 * letter (ä ö ü Ä Ö Ü ß) in JSX text or in ANY string or template literal of a
 * .tsx file (attribute, prop, ternary, constant) is a string that can show
 * German on an English screen. This guard finds them — strict on purpose.
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

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) return name === "__tests__" || name === "node_modules" ? [] : walk(p);
    return name.endsWith(".tsx") && !name.endsWith(".test.tsx") ? [p] : [];
  });
}

/**
 * One pass over the source: comments are blanked (line breaks kept), and every
 * string, template-literal piece and regex literal is cut out. String and
 * template text goes to `literals`; in `code` all of them are blanked, so JSX
 * text is looked for in `code` only and nothing is counted twice.
 * - `${…}` inside a template is code again (its own strings/comments count).
 * - A '…' or "…" without its closing quote on the same line is not a string
 *   (an apostrophe in JSX text, "Don't"), and the same holds for a "/" that
 *   does not close as a regex on its line (JSX "</div>", "a / b").
 */
export function splitSource(src) {
  let code = "";
  const literals = [];
  let line = 1;
  let i = 0;
  let depth = 0; // { } depth in code
  const templates = []; // depth at which each open `${` resumes its template
  let last = ""; // last non-space code char (regex vs. division)
  const blank = (chunk) => {
    code += chunk.replace(/[^\n]/g, " ");
    line += (chunk.match(/\n/g) || []).length;
  };
  // Reads template text from i up to "`" (end) or "${" (expression).
  const template = () => {
    const startLine = line;
    let text = "";
    while (i < src.length) {
      const ch = src[i];
      if (ch === "\\") { text += src.slice(i, i + 2); i += 2; continue; }
      if (ch === "`") {
        literals.push({ line: startLine, text });
        blank(text);
        code += "`";
        i++;
        last = "a";
        return;
      }
      if (ch === "$" && src[i + 1] === "{") {
        literals.push({ line: startLine, text });
        blank(text);
        code += "${";
        i += 2;
        templates.push(depth);
        depth++;
        last = "{";
        return;
      }
      text += ch;
      i++;
    }
    literals.push({ line: startLine, text });
    blank(text);
  };
  while (i < src.length) {
    const c = src[i];
    const n = src[i + 1];
    if (c === "/" && n === "*") {
      const end = src.indexOf("*/", i + 2);
      const stop = end === -1 ? src.length : end + 2;
      blank(src.slice(i, stop));
      i = stop;
      continue;
    }
    if (c === "/" && n === "/" && src[i - 1] !== ":") {
      const end = src.indexOf("\n", i);
      const stop = end === -1 ? src.length : end;
      code += " ".repeat(stop - i);
      i = stop;
      continue;
    }
    if (c === "/" && n !== ">" && src[i - 1] !== "<" && !/[\w$)\]"'`]/.test(last)) {
      // Regex literal, if it closes on this line.
      let j = i + 1;
      let inClass = false;
      while (j < src.length && src[j] !== "\n") {
        if (src[j] === "\\") { j += 2; continue; }
        if (src[j] === "[") inClass = true;
        else if (src[j] === "]") inClass = false;
        else if (src[j] === "/" && !inClass) break;
        j++;
      }
      if (src[j] === "/") {
        j++;
        while (/[a-z]/.test(src[j] ?? "")) j++;
        code += "/" + " ".repeat(j - i - 2) + "/";
        i = j;
        last = "a";
        continue;
      }
    }
    if (c === '"' || c === "'") {
      let j = i + 1;
      while (j < src.length && src[j] !== c && src[j] !== "\n") j += src[j] === "\\" ? 2 : 1;
      if (src[j] === c) {
        const text = src.slice(i + 1, j);
        literals.push({ line, text });
        code += c + " ".repeat(text.length) + c;
        i = j + 1;
        last = "a";
        continue;
      }
    }
    if (c === "`") {
      code += "`";
      i++;
      template();
      continue;
    }
    if (c === "}" && templates.length && templates[templates.length - 1] === depth - 1) {
      templates.pop();
      depth--;
      code += "}";
      i++;
      template();
      continue;
    }
    if (c === "{") depth++;
    if (c === "}") depth--;
    if (c === "\n") line++;
    if (!/\s/.test(c)) last = c;
    code += c;
    i++;
  }
  return { code, literals };
}

/**
 * Findings in one source text: [{ line, kind, text }].
 * - "string": ANY string or template literal with a German letter — attribute,
 *   prop, ternary, constant, notify() text alike. Strict on purpose: German
 *   belongs in messages/<locale>.json, nowhere in .tsx code.
 * - "jsx": JSX text, i.e. words written between tags.
 */
export function scanSource(src) {
  const { code, literals } = splitSource(src);
  const hits = [];
  for (const lit of literals) {
    if (GERMAN.test(lit.text)) hits.push({ line: lit.line, kind: "string", text: lit.text.trim() });
  }
  const lines = code.split("\n");
  lines.forEach((line, idx) => {
    if (!GERMAN.test(line)) return;
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
  return hits.sort((a, b) => a.line - b.line);
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

// Argument parsing and Markdown report for the UI probe (pure, testable).
import { tmpdir } from "node:os";
import { join } from "node:path";
import { rankFindings } from "./findings.mjs";

export const DEFAULT_WIDTHS = [1440, 390];
export const THEMES = ["dark", "light", "system"];

/** Parse CLI args: --base URL --out DIR --route /x (repeatable) --width 390 (repeatable or comma list). */
export function parseArgs(argv, now = new Date()) {
  const opts = { base: "http://localhost", out: null, routes: [], widths: [], maxPerPage: 0, headed: false, allRepeats: false, nestedMax: 20, loadTimeoutMs: 20000, theme: null, contrast: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = () => {
      const v = argv[++i];
      if (v === undefined || v.startsWith("--")) throw new Error(`missing value for ${a}`);
      return v;
    };
    if (a === "--base") opts.base = next().replace(/\/$/, "");
    else if (a === "--out") opts.out = next();
    else if (a === "--route") opts.routes.push(...next().split(",").map((s) => s.trim()).filter(Boolean));
    else if (a === "--width") opts.widths.push(...next().split(",").map((s) => Number(s.trim())));
    else if (a === "--max-per-page") opts.maxPerPage = Number(next());
    else if (a === "--headed") opts.headed = true;
    else if (a === "--all-repeats") opts.allRepeats = true;
    else if (a === "--nested-max") opts.nestedMax = Number(next());
    else if (a === "--load-timeout") opts.loadTimeoutMs = Number(next());
    else if (a === "--theme") opts.theme = next();
    else if (a === "--contrast") opts.contrast = true;
    else if (a === "--help" || a === "-h") opts.help = true;
    else throw new Error(`unknown argument: ${a}`);
  }
  if (opts.widths.length === 0) opts.widths = [...DEFAULT_WIDTHS];
  if (opts.widths.some((w) => !Number.isFinite(w) || w < 200 || w > 4000)) throw new Error("--width must be between 200 and 4000");
  if (!Number.isFinite(opts.nestedMax) || opts.nestedMax < 0) throw new Error("--nested-max must be 0 or more");
  if (!Number.isFinite(opts.loadTimeoutMs) || opts.loadTimeoutMs < 0) throw new Error("--load-timeout must be 0 or more (ms)");
  if (opts.theme !== null && !THEMES.includes(opts.theme)) throw new Error(`--theme must be one of ${THEMES.join(", ")}`);
  if (!opts.out) {
    const stamp = now.toISOString().replace(/[:.]/g, "-").slice(0, 19);
    opts.out = join(tmpdir(), "mc-ui-probe", stamp);
  }
  return opts;
}

/**
 * One "family" per repeated component: per-item parts of the label (the text
 * after "Name: " and every number) are folded away, so "Actions: Alpha" and
 * "Actions: Beta" or "Task 7 menu" and "Task 12 menu" land together.
 */
export function familyKey(c) {
  const label = String(c.label || "")
    .replace(/:\s.*$/, ": *")
    .replace(/\d+/g, "#");
  return `${c.tag}|${c.role || ""}|${label}`;
}

/**
 * Repeated components (the same menu on every row) are sampled like the
 * manual audit did: the first and the last of a family are probed, the middle
 * is skipped and counted. Tabs are never sampled — each shows its own view.
 */
export function sampleRepeats(cands) {
  const fam = new Map();
  cands.forEach((c, i) => {
    if ((c.role || "") === "tab") return;
    const k = familyKey(c);
    if (!fam.has(k)) fam.set(k, []);
    fam.get(k).push(i);
  });
  const drop = new Set();
  for (const idx of fam.values()) {
    if (idx.length > 2) idx.slice(1, -1).forEach((i) => drop.add(i));
  }
  return { keep: cands.filter((_, i) => !drop.has(i)), skipped: cands.filter((_, i) => drop.has(i)) };
}

/** Filesystem-safe short slug. */
export function slug(s, max = 40) {
  const x = String(s || "")
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, max);
  return x || "x";
}

const pct = (a, b) => (b ? `${Math.round((a / b) * 100)}%` : "–");

/** Render the Markdown report from the JSON result. */
export function renderMarkdown(result) {
  const L = [];
  L.push(`# UI probe report`);
  L.push("");
  L.push(`- Base: ${result.base}`);
  L.push(`- Started: ${result.startedAt} · finished: ${result.finishedAt}`);
  L.push(`- Widths: ${result.widths.join(", ")}`);
  if (result.theme) L.push(`- Theme: ${result.theme} (forced via local storage before the first paint)`);
  if (result.contrast) {
    const sum = (k) => result.pages.reduce((a, p) => a + (p.counts[k] || 0), 0);
    L.push(`- Contrast (WCAG AA text): ${sum("contrastChecked")} text element(s) checked, ${sum("contrastUncertain")} skipped (background image/gradient)`);
  }
  L.push(`- **Write lock:** ${result.writes.blocked} write request(s) blocked, **${result.writes.passed} passed**, ${result.writes.websocketsRefused} WebSocket(s) refused`);
  if (result.skippedRoutes.length) {
    L.push(`- Skipped routes: ${result.skippedRoutes.map((s) => `\`${s.pattern}\` (${s.reason})`).join("; ")}`);
  }
  L.push("");
  L.push("## Opened per page");
  L.push("");
  L.push("| Page | Width | Opened | Candidates | Rate | Read (select) | Nested | Sampled out | Guarded | No change | Left page | Failed | Findings |");
  L.push("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|");
  for (const p of result.pages) {
    const c = p.counts;
    const n = (v) => v ?? 0;
    const path = c.loading ? `\`${p.path}\` (still loading)` : `\`${p.path}\``;
    L.push(`| ${path} | ${p.width} | ${n(c.opened)} | ${n(c.candidates)} | ${pct(n(c.opened), c.candidates)} | ${n(c.read)} | ${n(c.nestedOpened)} | ${n(c.sampledOut)} | ${n(c.guarded)} | ${n(c.noChange)} | ${n(c.navigated)} | ${n(c.failed)} | ${p.findings.length} |`);
  }
  L.push("");
  L.push("Opened = a click produced a new visible state (a layer, an expanded section or a switched tab). Read (select) = native selects whose options were read, not counted as opened. Nested = controls opened inside what a first click revealed (second level). Sampled out = repeats of the same component (e.g. the same row menu), only the first and last are probed; `--all-repeats` probes all. Guarded = on the never-click list. Candidates without an ARIA role are counted in `withoutAriaRole` in the JSON.");
  L.push("");
  const all = rankFindings(result.pages.flatMap((p) => p.findings));
  L.push(`## Findings (${all.length})`);
  L.push("");
  if (!all.length) L.push("None.");
  for (const f of all) {
    const ops = f.openers && f.openers.length ? f.openers : f.opener ? [f.opener] : [];
    const shown = ops.slice(0, 6).map((o) => `"${o}"`).join(", ");
    const where = ops.length ? ` after opening ${shown}${ops.length > 6 ? ` +${ops.length - 6} more` : ""}` : " (page as loaded)";
    const times = f.count > 1 ? ` ×${f.count}` : "";
    L.push(`- **${f.severity}** \`${f.type}\` — \`${f.page}\` @${f.width}${where}: ${f.message}${times}`);
    const d = f.detail || {};
    const extra = [d.examples && `examples: ${d.examples.join(" · ")}`, d.hits && d.hits.length && `hit: ${d.hits.join(", ")}`, d.offenders && d.offenders.length && `offenders: ${d.offenders.join(" · ")}`].filter(Boolean);
    if (extra.length) L.push(`  - ${extra.join(" — ")}`);
    if (f.shots && f.shots.length) L.push(`  - shots: ${f.shots.map((s) => `\`${s}\``).join(", ")}`);
  }
  L.push("");
  L.push("## Guarded controls (never clicked)");
  L.push("");
  for (const p of result.pages) {
    const g = p.states.filter((s) => s.status === "guarded");
    if (!g.length) continue;
    L.push(`- \`${p.path}\` @${p.width}: ${g.map((s) => `"${s.label}" (${s.reason.replace("guarded:", "")})`).join(", ")}`);
  }
  if (result.writes.samples.length) {
    L.push("");
    L.push("## Blocked write requests (sample)");
    L.push("");
    for (const w of result.writes.samples) L.push(`- ${w}`);
  }
  L.push("");
  return L.join("\n");
}

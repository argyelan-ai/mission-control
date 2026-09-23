// Argument parsing and Markdown report for the UI probe (pure, testable).
import { tmpdir } from "node:os";
import { join } from "node:path";
import { rankFindings } from "./findings.mjs";

export const DEFAULT_WIDTHS = [1440, 390];

/** Parse CLI args: --base URL --out DIR --route /x (repeatable) --width 390 (repeatable or comma list). */
export function parseArgs(argv, now = new Date()) {
  const opts = { base: "http://localhost", out: null, routes: [], widths: [], maxPerPage: 0, headed: false };
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
    else if (a === "--help" || a === "-h") opts.help = true;
    else throw new Error(`unknown argument: ${a}`);
  }
  if (opts.widths.length === 0) opts.widths = [...DEFAULT_WIDTHS];
  if (opts.widths.some((w) => !Number.isFinite(w) || w < 200 || w > 4000)) throw new Error("--width must be between 200 and 4000");
  if (!opts.out) {
    const stamp = now.toISOString().replace(/[:.]/g, "-").slice(0, 19);
    opts.out = join(tmpdir(), "mc-ui-probe", stamp);
  }
  return opts;
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
  L.push(`- **Write lock:** ${result.writes.blocked} write request(s) blocked, **${result.writes.passed} passed**, ${result.writes.websocketsRefused} WebSocket(s) refused`);
  if (result.skippedRoutes.length) {
    L.push(`- Skipped routes: ${result.skippedRoutes.map((s) => `\`${s.pattern}\` (${s.reason})`).join("; ")}`);
  }
  L.push("");
  L.push("## Opened per page");
  L.push("");
  L.push("| Page | Width | Opened | Candidates | Rate | Guarded | No change | Left page | Failed | Nested | Findings |");
  L.push("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|");
  for (const p of result.pages) {
    const c = p.counts;
    L.push(`| \`${p.path}\` | ${p.width} | ${c.opened} | ${c.candidates} | ${pct(c.opened, c.candidates)} | ${c.guarded} | ${c.noChange} | ${c.navigated} | ${c.failed} | ${c.nestedOpened} | ${p.findings.length} |`);
  }
  L.push("");
  L.push("Opened = a click (or reading a native select) produced a new visible state. Guarded = on the never-click list. Candidates without an ARIA role are counted in `withoutAriaRole` in the JSON.");
  L.push("");
  const all = rankFindings(result.pages.flatMap((p) => p.findings));
  L.push(`## Findings (${all.length})`);
  L.push("");
  if (!all.length) L.push("None.");
  for (const f of all) {
    const where = f.opener ? ` after opening "${f.opener}"` : " (page as loaded)";
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

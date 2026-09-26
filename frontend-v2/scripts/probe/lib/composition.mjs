// Composition budget for one region of a page (DESIGN.md "Komposition",
// rules K1–K14). Pure functions over what the browser measured, so the
// counting logic is unit-tested without Playwright. The collector at the
// bottom runs inside the page (page.evaluate) and must stay self-contained.
//
// The numbers are a floor, not a goal (rule K14): green means "did not fail",
// never "looks good". The operator's look at the phone picture decides.

/** Default limits for a detail-page header region (DESIGN.md K4–K9). */
export const HEAD_LIMITS = Object.freeze({
  maxFontSizes: 3, // K5: at most 3 sizes in a header
  maxWeights: 2, // K5: at most 2 weights per region
  maxTextColors: 4, // K6: primary/secondary/muted + 1 status colour
  maxUppercase: 0, // K7: no uppercase label in a header
  maxLeftEdges: 2, // K9: one main edge + one indent
  maxBoxDepth: 1, // K8: at most one surface in a header
  minFontPx: 11, // K5: nothing tinier
  maxItems: 5, // K4: at most 5 facts in a header
  minRepeatLength: 6, // K3: repeats shorter than this are normal ("·", "min", names)
});

const LETTERS = /[A-Za-zÄÖÜäöüß]/g;

/** True when a piece of text reads as an uppercase label ("STATUS", "AUFGABE · C6C8553E"). */
export function isUppercaseLabel(node) {
  if (node.textTransform === "uppercase") return (node.text.match(LETTERS) || []).length >= 2;
  const letters = (node.text.match(LETTERS) || []).join("");
  return letters.length >= 3 && letters === letters.toUpperCase() && letters !== letters.toLowerCase();
}

/** Distinct values after rounding numbers (sub-pixel noise is not a new size). */
const distinct = (values) => [...new Set(values)].sort((a, b) => (a > b ? 1 : a < b ? -1 : 0));

/** Group left positions that lie within `tol` px of each other into one edge. */
export function clusterEdges(lefts, tol = 2) {
  const sorted = [...lefts].sort((a, b) => a - b);
  const edges = [];
  for (const x of sorted) {
    const last = edges[edges.length - 1];
    if (last !== undefined && x - last <= tol) continue;
    edges.push(x);
  }
  return edges;
}

/** Normalise a computed colour so alpha variants of the same token count once. */
export function colorKey(css) {
  const m = /rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/.exec(css || "");
  if (!m) return String(css || "").trim();
  return "#" + [m[1], m[2], m[3]].map((n) => Number(n).toString(16).padStart(2, "0")).join("");
}

/**
 * Count what a region shows and compare it with the limits.
 * @param {Array<{text:string, fontSize:number, fontWeight:number, color:string,
 *   left:number, lineStart?:boolean, textTransform?:string, truncated?:boolean,
 *   control?:boolean}>} nodes  control = label inside a button/link/field: it counts for
 *   sizes and weights, but not as a text colour or a left edge (the control's box is the edge)
 * @param {{boxDepth?:number, items?:number}} extra  measured outside the text nodes
 */
export function compositionBudget(nodes, extra = {}, limits = HEAD_LIMITS) {
  const visible = nodes.filter((n) => (n.text || "").trim().length > 0);
  const sizes = distinct(visible.map((n) => Math.round(n.fontSize)));
  const weights = distinct(visible.map((n) => Number(n.fontWeight)));
  const text = visible.filter((n) => !n.control);
  const colors = distinct(text.map((n) => colorKey(n.color)));
  const uppercase = visible.filter(isUppercaseLabel).map((n) => n.text.trim());
  const edges = clusterEdges(text.filter((n) => n.lineStart !== false).map((n) => Math.round(n.left)));
  const tiny = visible.filter((n) => n.fontSize < limits.minFontPx).map((n) => n.text.trim());
  // The title (largest text) may be cut after its line clamp; nothing else may (K3).
  const maxSize = Math.max(0, ...visible.map((n) => n.fontSize));
  const truncated = visible
    .filter((n) => n.fontSize < maxSize && (n.truncated || /…$|\.\.\.$/.test(n.text.trim())))
    .map((n) => n.text.trim());

  const seen = new Map();
  for (const n of visible) {
    const t = n.text.trim().replace(/\s+/g, " ");
    if (t.length < limits.minRepeatLength) continue;
    seen.set(t, (seen.get(t) || 0) + 1);
  }
  const repeats = [...seen.entries()].filter(([, c]) => c > 1).map(([t, c]) => ({ text: t, count: c }));

  const metrics = {
    textPieces: visible.length,
    fontSizes: sizes,
    fontWeights: weights,
    textColors: colors,
    uppercase,
    leftEdges: edges,
    tinyText: tiny,
    truncated,
    repeats,
    boxDepth: extra.boxDepth ?? null,
    items: extra.items ?? null,
  };

  const findings = [];
  const over = (rule, what, value, limit) => findings.push({ rule, what, value, limit });
  if (sizes.length > limits.maxFontSizes) over("K5", "font sizes", sizes.length, limits.maxFontSizes);
  if (weights.length > limits.maxWeights) over("K5", "font weights", weights.length, limits.maxWeights);
  if (tiny.length) over("K5", `text under ${limits.minFontPx}px`, tiny.length, 0);
  if (colors.length > limits.maxTextColors) over("K6", "text colours", colors.length, limits.maxTextColors);
  if (uppercase.length > limits.maxUppercase) over("K7", "uppercase labels", uppercase.length, limits.maxUppercase);
  if (edges.length > limits.maxLeftEdges) over("K9", "left edges", edges.length, limits.maxLeftEdges);
  if (repeats.length) over("K3", "same text shown twice", repeats.length, 0);
  if (truncated.length) over("K3", "cut-off texts besides the title", truncated.length, 0);
  if (metrics.boxDepth != null && metrics.boxDepth > limits.maxBoxDepth) over("K8", "nested surfaces", metrics.boxDepth, limits.maxBoxDepth);
  if (metrics.items != null && metrics.items > limits.maxItems) over("K4", "facts in the header", metrics.items, limits.maxItems);

  return { metrics, findings, passed: findings.length === 0 };
}

/**
 * Runs in the browser. Collects the text pieces of one region plus the
 * deepest nesting of visible surfaces (background or border) inside it.
 * Elements marked `data-fact` count as one fact each (K4) when present.
 * `firstScreen` keeps only what is visible without scrolling.
 * Self-contained on purpose — Playwright serialises it.
 */
export function collectRegion({ selector, firstScreen = false }) {
  const root = document.querySelector(selector);
  if (!root) return { error: `region not found: ${selector}` };
  const rootRect = root.getBoundingClientRect();
  const nodes = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let lastLeftTop = null;
  for (let t = walker.nextNode(); t; t = walker.nextNode()) {
    const text = t.textContent.replace(/\s+/g, " ").trim();
    if (!text) continue;
    const el = t.parentElement;
    const cs = getComputedStyle(el);
    if (cs.visibility === "hidden" || cs.display === "none" || Number(cs.opacity) === 0) continue;
    const range = document.createRange();
    range.selectNodeContents(t);
    const r = range.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (r.bottom < rootRect.top || r.top > rootRect.bottom) continue;
    if (firstScreen && (r.top >= window.innerHeight || r.bottom <= 0)) continue;
    // A piece starts a line when nothing before it sits on the same line.
    const lineStart = lastLeftTop === null || Math.abs(r.top - lastLeftTop) > r.height / 2;
    if (lineStart) lastLeftTop = r.top;
    nodes.push({
      text,
      fontSize: parseFloat(cs.fontSize),
      fontWeight: Number(cs.fontWeight) || 400,
      color: cs.color,
      left: r.left - rootRect.left,
      lineStart,
      textTransform: cs.textTransform,
      truncated: el.scrollWidth > el.clientWidth + 1 && cs.textOverflow === "ellipsis",
      control: !!el.closest("button,a,input,select,textarea,[role=button],[role=tab],[role=switch]"),
    });
  }
  const boxed = (el) => {
    if (el.matches("button,a,input,select,textarea,[role=button],[role=switch]")) return false; // controls are not surfaces
    const cs = getComputedStyle(el);
    const bg = cs.backgroundColor;
    const hasBg = bg && bg !== "transparent" && !/rgba\([^)]*,\s*0\)$/.test(bg);
    const hasBorder = ["Top", "Right", "Bottom", "Left"].filter((s) => parseFloat(cs[`border${s}Width`]) > 0 && cs[`border${s}Style`] !== "none").length >= 3;
    const r = el.getBoundingClientRect();
    return (hasBg || hasBorder) && r.width > 120 && r.height > 32;
  };
  let boxDepth = 0;
  const walk = (el, depth) => {
    const d = el !== root && boxed(el) ? depth + 1 : depth;
    boxDepth = Math.max(boxDepth, d);
    for (const c of el.children) walk(c, d);
  };
  walk(root, 0);
  const facts = root.querySelectorAll("[data-fact]").length;
  return { nodes, boxDepth, items: facts || null, height: Math.round(rootRect.height) };
}

/**
 * CLI arguments of budget.mjs:
 *   --url URL --region CSS [--width 390,1440] [--out DIR] [--light] [--strict] [--wait CSS] [--first-screen]
 */
export function parseBudgetArgs(argv) {
  const opts = { url: "", region: "", widths: [], out: "", light: false, strict: false, wait: "", firstScreen: false, help: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = () => {
      const v = argv[++i];
      if (v === undefined || v.startsWith("--")) throw new Error(`missing value for ${a}`);
      return v;
    };
    if (a === "--url") opts.url = next();
    else if (a === "--region") opts.region = next();
    else if (a === "--width") opts.widths.push(...next().split(",").map((s) => Number(s.trim())));
    else if (a === "--out") opts.out = next();
    else if (a === "--wait") opts.wait = next();
    else if (a === "--light") opts.light = true;
    else if (a === "--strict") opts.strict = true;
    else if (a === "--first-screen") opts.firstScreen = true;
    else if (a === "--help" || a === "-h") opts.help = true;
    else throw new Error(`unknown argument ${a}`);
  }
  if (opts.help) return opts;
  if (!opts.url) throw new Error("--url is required");
  if (!opts.region) throw new Error("--region is required");
  if (!opts.widths.length) opts.widths = [390, 1440];
  if (opts.widths.some((w) => !Number.isFinite(w) || w < 200 || w > 4000)) throw new Error("--width must be between 200 and 4000");
  return opts;
}

/**
 * Which build a URL serves. A branch must be measured on its own dev server
 * (`npm run dev`, port 3001) — http://localhost is the deployed main, so a
 * green budget there says nothing about the branch.
 */
export function measuredBuild(url) {
  let u;
  try {
    u = new URL(url);
  } catch {
    return { kind: "unknown", warning: "not a URL" };
  }
  if (u.protocol === "file:") return { kind: "file", warning: "" };
  const local = ["localhost", "127.0.0.1"].includes(u.hostname);
  if (local && u.port === "3001") return { kind: "dev-server", warning: "" };
  if (local) return { kind: "deployed", warning: "this is the deployed build, not your branch — run `npm run dev` and measure :3001" };
  return { kind: "remote", warning: "remote URL — make sure it serves the commit you are reporting" };
}

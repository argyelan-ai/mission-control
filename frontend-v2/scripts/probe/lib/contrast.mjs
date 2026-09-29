// Text-contrast heuristic for the UI probe (WCAG 2.x AA, 1.4.3) — pure maths
// over what browser.mjs measureContrast() collected, so it can be unit-tested.
//
// A sample is one element with its own visible text:
//   { text, fg: [r,g,b,a], chain: [{ bg: [r,g,b,a], op, img? }, …], fontPx, weight }
// `chain` runs from the text's element up to <html>: every ancestor's own
// background colour and opacity (the probe drops entries that change nothing).
// Opacity applies to an element's whole group (its background, the text and
// everything between), exactly like the browser composites it.
//
// Limits (honest): what really sits *behind* a positioned element is not
// always its DOM ancestor, and background images / gradients cannot be judged
// from colours — such samples are skipped and counted as "uncertain".

export const AA_NORMAL = 4.5;
export const AA_LARGE = 3;
/** Below this combined opacity text is hidden (or fading in/out), not "dim". */
export const HIDDEN_OPACITY = 0.15;

const lin = (c) => {
  const s = c / 255;
  return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
};
const luminance = ([r, g, b]) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);

/** WCAG contrast ratio of two opaque sRGB colours (0–255). */
export function contrastRatio(a, b) {
  const la = luminance(a);
  const lb = luminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

// premultiplied rgba helpers
const pm = ([r, g, b, a]) => [r * a, g * a, b * a, a];
const scale = (p, k) => p.map((v) => v * k);
const over = (src, dst) => src.map((v, i) => v + dst[i] * (1 - src[3]));

/**
 * Final on-screen colour of the text and of the background right behind it.
 * groupColor(0) = text over the element's own background; each ancestor adds
 * its background underneath and fades the group by its opacity; the page sits
 * on a white canvas.
 */
export function renderTextPixel(fg, chain) {
  const render = (text) => {
    let g = text ? pm(fg) : [0, 0, 0, 0];
    for (let i = 0; i < chain.length; i++) {
      const { bg, op } = chain[i];
      g = over(g, pm(bg || [0, 0, 0, 0]));
      g = scale(g, op ?? 1);
    }
    const out = over(g, [255, 255, 255, 1]);
    return out.slice(0, 3);
  };
  return { text: render(true), bg: render(false) };
}

export const toHex = (c) =>
  "#" +
  c
    .slice(0, 3)
    .map((v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0"))
    .join("")
    .toUpperCase();

/**
 * A background image/gradient matters only if no opaque background colour
 * sits between it and the text (a page texture under an opaque card does not).
 */
export function imageBehind(chain) {
  for (const c of chain) {
    if (c.img) return true;
    if (c.bg && c.bg[3] >= 1) return false;
  }
  return false;
}

/** WCAG "large text": at least 24px, or at least 18.66px (14pt) and bold. */
export const isLargeText = (px, weight) => px >= 24 || (px >= 18.66 && weight >= 700);

/**
 * Judge all samples of one state. Failing samples are grouped per colour pair
 * (one finding each, with a few example texts) so a repeated style is one
 * finding, not hundreds.
 */
export function contrastFindings(samples) {
  let checked = 0;
  let uncertain = 0;
  let hidden = 0;
  const groups = new Map();
  for (const s of samples || []) {
    if (!s || !s.fg || !(s.fg[3] > 0)) continue;
    if ((s.chain || []).reduce((a, c) => a * (c.op ?? 1), 1) < HIDDEN_OPACITY) {
      hidden += 1;
      continue;
    }
    if (imageBehind(s.chain || []) || s.uncertain) {
      uncertain += 1;
      continue;
    }
    checked += 1;
    const px = renderTextPixel(s.fg, s.chain || []);
    const ratio = contrastRatio(px.text, px.bg);
    const large = isLargeText(s.fontPx || 0, s.weight || 400);
    const need = large ? AA_LARGE : AA_NORMAL;
    if (ratio >= need) continue;
    const key = `${toHex(px.text)}|${toHex(px.bg)}|${need}`;
    const g = groups.get(key) || { fg: toHex(px.text), bg: toHex(px.bg), ratio, need, large, examples: [], count: 0 };
    g.count += 1;
    if (g.examples.length < 3) g.examples.push(String(s.text || "").slice(0, 40));
    groups.set(key, g);
  }
  const findings = [...groups.values()].map((g) => ({
    type: "contrast",
    severity: "high",
    message: `${g.large ? "large text" : "text"} ${g.fg} on ${g.bg} = ${Math.floor(g.ratio * 100) / 100}:1 (AA needs ${g.need}:1)`,
    detail: { examples: g.examples, count: g.count, ratio: g.ratio },
  }));
  return { findings, checked, uncertain, hidden };
}

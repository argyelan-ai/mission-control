// In-page measurement code for the UI probe. Every exported function here is
// passed to page.evaluate(), so it must be self-contained (no outer scope).

/**
 * Tag every visible opener candidate with data-probe-c (or `attr`) and
 * describe it. `within` (optional CSS selector) limits collection to one
 * subtree; `onlyNew` keeps only elements that appeared after markSeen() —
 * the controls a first click revealed (second level).
 */
export function collectCandidates(args) {
  const { within, ariaOnly, onlyNew } = args || {};
  const SEL = ariaOnly
    ? '[role=tab], [aria-expanded="false"], summary'
    : 'button, select, [role=combobox], [aria-haspopup]:not([aria-haspopup="false"]), [aria-expanded="false"], summary, [role=tab]';
  const root = within ? document.querySelector(within) : document;
  if (!root) return [];
  const vis = (e) => {
    const r = e.getBoundingClientRect();
    const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none" && !(s.opacity !== "" && +s.opacity === 0);
  };
  const labelOf = (e) =>
    (e.getAttribute("aria-label") || e.innerText || e.getAttribute("title") || e.getAttribute("placeholder") || e.getAttribute("name") || "")
      .trim()
      .replace(/\s+/g, " ")
      .slice(0, 60);
  const els = [...new Set(root.querySelectorAll(SEL))].filter(vis).filter((e) => !onlyNew || !e.hasAttribute("data-probe-seen"));
  const attr = (args && args.attr) || (ariaOnly ? "data-probe-n" : "data-probe-c");
  document.querySelectorAll(`[${attr}]`).forEach((e) => e.removeAttribute(attr));
  const seenKeys = {};
  return els.map((e, i) => {
    e.setAttribute(attr, String(i));
    const role = e.getAttribute("role");
    const label = labelOf(e);
    const tag = e.tagName.toLowerCase();
    const key = `${tag}|${role || ""}|${label}`;
    seenKeys[key] = (seenKeys[key] || 0) + 1;
    // For tabs: the key of the tab that is active in the same tab list, so
    // the probe can switch back after looking at this one.
    let restoreKey = null;
    if (role === "tab" && e.getAttribute("aria-selected") !== "true") {
      const list = e.closest('[role=tablist]') || e.parentElement;
      const active = list && list.querySelector('[role=tab][aria-selected="true"]');
      if (active) restoreKey = `${active.tagName.toLowerCase()}|tab|${labelOf(active)}`;
    }
    return {
      i,
      key,
      restoreKey,
      ordinal: seenKeys[key] - 1,
      tag,
      role,
      // A <button> without a type attribute inside a form IS a submit button
      // (the property says so, the attribute does not). Outside a form it
      // cannot submit anything, whatever its default type.
      type: tag === "button" ? (e.form ? e.type : e.getAttribute("type") || "button") : e.getAttribute("type"),
      label,
      expanded: e.getAttribute("aria-expanded"),
      haspopup: e.getAttribute("aria-haspopup"),
      selected: e.getAttribute("aria-selected"),
      disabled: !!(e.disabled || e.getAttribute("aria-disabled") === "true"),
      scope: e.closest("main") ? "page" : "shell",
      hasAriaRole: !!(role || e.hasAttribute("aria-expanded") || e.hasAttribute("aria-haspopup") || tag === "select" || tag === "summary"),
    };
  });
}

/**
 * Is the page still loading? Visible short "Loading…" texts (pattern passed in
 * from findings.mjs LOADING_TEXT_RE) and visible [aria-busy=true] regions.
 */
export function pageBusy(args) {
  const re = new RegExp(args.pattern, args.flags || "iu");
  const vis = (e) => {
    const r = e.getBoundingClientRect();
    const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  };
  const hints = [];
  const walker = document.createTreeWalker(document.querySelector("main") || document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n && hints.length < 5; n = walker.nextNode()) {
    const t = (n.parentElement && n.parentElement.innerText ? n.parentElement.innerText : n.textContent || "").trim().replace(/\s+/g, " ");
    if (t.length <= 60 && re.test(t) && n.parentElement && vis(n.parentElement) && !hints.includes(t)) hints.push(t);
  }
  const busy = [...document.querySelectorAll('[aria-busy="true"]')].filter(vis).length;
  if (busy) hints.push(`aria-busy x${busy}`);
  return hints;
}

/**
 * Mark every element currently in the DOM as "seen" (baseline for a click).
 * The second level uses its own attribute so the first level's baseline —
 * needed later to check whether Escape closed the parent — stays intact.
 */
export function markSeen(attr) {
  const a = attr || "data-probe-seen";
  document.querySelectorAll("*").forEach((e) => e.setAttribute(a, "1"));
}

/** Read option labels of a native <select> (cannot be screenshotted open). */
export function readSelect(sel) {
  const s = document.querySelector(sel);
  if (!s) return null;
  return { value: s.value, options: [...s.options].map((o) => o.textContent.trim().slice(0, 80)) };
}

/**
 * Measure the page: new layers since markSeen(), sideways overflow, targets.
 * @param {{opener?: string, base?: boolean}} args
 */
export function measureState(args) {
  const { opener, base } = args || {};
  const SEEN = (args && args.seenAttr) || "data-probe-seen";
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const vis = (e) => {
    const r = e.getBoundingClientRect();
    const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none" && !(s.opacity !== "" && +s.opacity === 0) && e.getAttribute("data-state") !== "closed";
  };
  const labelOf = (e) =>
    (e.getAttribute("aria-label") || e.innerText || e.getAttribute("title") || e.getAttribute("placeholder") || "")
      .trim()
      .replace(/\s+/g, " ")
      .slice(0, 50);
  const INTER = 'button, a[href], input:not([type=hidden]), select, textarea, [role=button], [role=tab], [role=menuitem], [role=option], [role=switch], [role=checkbox], summary';
  const targetsIn = (root) =>
    [...root.querySelectorAll(INTER)].filter(vis).map((e) => {
      const r = e.getBoundingClientRect();
      return { label: labelOf(e), w: r.width, h: r.height };
    });
  const isFixed = (e) => {
    for (let n = e; n && n !== document.body; n = n.parentElement) {
      if (getComputedStyle(n).position === "fixed") return true;
    }
    return false;
  };
  const describe = (e) => {
    const cls = (e.className && e.className.toString ? e.className.toString() : "").trim().split(/\s+/).slice(0, 3).join(".");
    return `${e.tagName.toLowerCase()}${cls ? "." + cls : ""}`;
  };

  // New layer roots: unseen, visible, big enough, parent was seen.
  const layers = [];
  if (!base) {
    const roots = [...document.querySelectorAll(`body *:not([${SEEN}])`)].filter((e) => {
      if (!e.parentElement || !e.parentElement.hasAttribute(SEEN) && e.parentElement !== document.body) return false;
      if (!vis(e)) return false;
      const r = e.getBoundingClientRect();
      return r.width * r.height >= 400;
    });
    roots.sort((a, b) => {
      const ra = a.getBoundingClientRect();
      const rb = b.getBoundingClientRect();
      return rb.width * rb.height - ra.width * ra.height;
    });
    for (const e of roots.slice(0, 8)) {
      const r = e.getBoundingClientRect();
      const s = getComputedStyle(e);
      const role = e.getAttribute("role") || (e.querySelector("[role=dialog],[role=menu],[role=listbox],[role=tooltip]") || {}).getAttribute?.("role") || null;
      const fixed = isFixed(e);
      const positioned = fixed || s.position === "absolute" || !!role;
      const kind = fixed || role === "dialog" ? (role || "overlay") : positioned ? (role || "popover") : "inline";
      // elementFromPoint probe (only for floating layers that take pointer events)
      const samples = [];
      const hitBy = [];
      if (kind !== "inline" && s.pointerEvents !== "none") {
        const pts = [
          [r.left + r.width / 2, r.top + r.height / 2],
          [r.left + r.width / 2, r.top + 6],
          [r.left + 6, r.top + r.height / 2],
          [r.right - 6, r.top + r.height / 2],
          [r.left + r.width / 2, r.bottom - 6],
        ];
        for (const [x, y] of pts) {
          const inViewport = x >= 0 && y >= 0 && x <= vw && y <= vh;
          if (!inViewport) { samples.push({ inViewport, inside: false }); continue; }
          const t = document.elementFromPoint(x, y);
          // inside = the topmost element is new (part of any new layer), not old page content
          const inside = !!t && (!t.hasAttribute(SEEN) || e.contains(t));
          if (!inside && t) hitBy.push(describe(t));
          samples.push({ inViewport, inside });
        }
      }
      // Cut off by an ancestor that hides overflow? The walk stops at the first
      // scroll container: content taller/wider than a scroller is normal.
      // Inline expansions are only judged horizontally (the page scrolls).
      // A position:fixed layer escapes overflow clipping unless an ancestor
      // creates a containing block for it (transform, filter, contain, …).
      const makesFixedContainer = (ns) =>
        ns.transform !== "none" || ns.filter !== "none" || ns.perspective !== "none" || ns.backdropFilter !== "none" && ns.backdropFilter !== undefined || /paint|layout|strict|content/.test(ns.contain || "") || /transform|filter|perspective/.test(ns.willChange || "");
      let clippedBy = null;
      let pastFixed = s.position === "fixed";
      for (let n = e.parentElement; n && n !== document.body; n = n.parentElement) {
        const ns = getComputedStyle(n);
        if (pastFixed && !makesFixedContainer(ns)) continue;
        if (ns.position === "fixed") pastFixed = true;
        const nr = n.getBoundingClientRect();
        const hidX = /(hidden|clip)/.test(ns.overflowX);
        const hidY = /(hidden|clip)/.test(ns.overflowY);
        if ((hidX && (r.left < nr.left - 1 || r.right > nr.right + 1)) || (kind !== "inline" && hidY && (r.top < nr.top - 1 || r.bottom > nr.bottom + 1))) {
          clippedBy = describe(n);
          break;
        }
        if (/(auto|scroll)/.test(ns.overflowX + ns.overflowY)) break;
      }
      const clipped = [...e.querySelectorAll("*")].filter((c) => {
        if (c.children.length || !c.textContent.trim() || !vis(c)) return false;
        const cs = getComputedStyle(c);
        return c.scrollWidth > c.clientWidth + 2 && /(hidden|clip)/.test(cs.overflowX) && cs.textOverflow !== "ellipsis";
      });
      const text = (e.innerText || "").trim();
      // A full-screen scrim behind a dialog/drawer: not content, not "empty".
      const backdrop = kind !== "inline" && !text && e.children.length === 0 && r.width * r.height >= 0.6 * vw * vh;
      layers.push({
        kind,
        backdrop,
        role,
        label: labelOf(e).slice(0, 40),
        selector: describe(e),
        rect: { left: Math.round(r.left), top: Math.round(r.top), right: Math.round(r.right), bottom: Math.round(r.bottom) },
        samples,
        hitBy: [...new Set(hitBy)].slice(0, 3),
        text: text.slice(0, 200),
        media: !!e.querySelector("img, svg, canvas, video"),
        interactive: e.querySelectorAll(INTER).length,
        clippedText: clipped.length,
        clippedExamples: clipped.slice(0, 3).map((c) => c.textContent.trim().slice(0, 40)),
        clippedBy,
        targets: targetsIn(e),
      });
    }
  }

  const sw = document.documentElement.scrollWidth;
  let overflowOffenders = [];
  if (sw > vw + 1) {
    overflowOffenders = [...document.querySelectorAll("body *")]
      .filter((e) => {
        if (!vis(e)) return false;
        const r = e.getBoundingClientRect();
        const p = e.parentElement && e.parentElement.getBoundingClientRect();
        return r.right > vw + 1 && (!p || p.right <= vw + 1);
      })
      .slice(0, 5)
      .map((e) => `${describe(e)} "${(e.innerText || "").trim().slice(0, 30)}" right=${Math.round(e.getBoundingClientRect().right)}`);
  }
  const main = document.querySelector("main") || document.body;
  const op = opener ? document.querySelector(opener) : null;
  return {
    viewport: { w: vw, h: vh },
    scrollWidth: sw,
    layers,
    overflowOffenders,
    targets: base ? targetsIn(document.body) : [],
    mainTextLength: (main.innerText || "").trim().length,
    mainInteractive: main.querySelectorAll(INTER).length,
    openerExpanded: op ? op.getAttribute("aria-expanded") : null,
    openerSelected: op ? op.getAttribute("aria-selected") : null,
    url: location.pathname + location.search,
    theme: document.documentElement.getAttribute("data-theme"),
  };
}

/**
 * Wait (up to `maxMs`) until no finite animation or transition is running —
 * a fade-in measured half-way shows text at half contrast. Endless animations
 * are ignored here (measureContrast marks their text as uncertain).
 */
export async function settleAnimations(args) {
  const maxMs = (args && args.maxMs) || 2000;
  const until = Date.now() + maxMs;
  const busy = () => {
    try {
      return document.getAnimations().some((a) => a.playState === "running" && a.effect && a.effect.getTiming().iterations !== Infinity);
    } catch {
      return false;
    }
  };
  while (busy() && Date.now() < until) await new Promise((r) => setTimeout(r, 100));
  return !busy();
}

/**
 * Collect text samples for the contrast heuristic (lib/contrast.mjs): every
 * visible element with its own letters/digits, its text colour and the chain
 * of background colours + opacities up to <html>. `base` = whole page; else
 * only elements that appeared since markSeen(seenAttr) (the opened state).
 * Colours are normalised through a 1×1 canvas, so oklch()/color-mix()/color()
 * values arrive as sRGB rgba. Disabled controls are exempt (WCAG 1.4.3).
 */
export function measureContrast(args) {
  const { base, max = 600 } = args || {};
  const SEEN = (args && args.seenAttr) || "data-probe-seen";
  const cv = document.createElement("canvas");
  cv.width = cv.height = 1;
  const cx = cv.getContext("2d", { willReadFrequently: true });
  const cache = new Map();
  const rgba = (c) => {
    if (cache.has(c)) return cache.get(c);
    let out = null;
    const m = /^rgba?\(([\d.]+)[, ]+([\d.]+)[, ]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\)$/.exec(c);
    if (m) {
      const a = m[4] === undefined ? 1 : m[4].endsWith("%") ? parseFloat(m[4]) / 100 : parseFloat(m[4]);
      out = [+m[1], +m[2], +m[3], a];
    } else if (cx) {
      cx.clearRect(0, 0, 1, 1);
      cx.fillStyle = "#010203";
      cx.fillStyle = c;
      if (cx.fillStyle !== "#010203" || c === "#010203") {
        cx.fillRect(0, 0, 1, 1);
        const d = cx.getImageData(0, 0, 1, 1).data;
        out = [d[0], d[1], d[2], d[3] / 255];
      }
    }
    cache.set(c, out);
    return out;
  };
  const vis = (e) => {
    const r = e.getBoundingClientRect();
    const s = getComputedStyle(e);
    return r.width > 1 && r.height > 1 && s.visibility !== "hidden" && s.display !== "none" && s.clip === "auto";
  };
  const own = (e) =>
    [...e.childNodes]
      .filter((n) => n.nodeType === 3)
      .map((n) => n.textContent)
      .join(" ")
      .trim()
      .replace(/\s+/g, " ");
  // Elements under an endless animation (pulsing "Loading…", shimmer): their
  // colour changes all the time, a snapshot says nothing → "uncertain".
  const looping = new Set();
  try {
    for (const a of document.getAnimations()) {
      const t = a.effect && a.effect.target;
      if (t && a.playState === "running" && a.effect.getTiming().iterations === Infinity) looping.add(t);
    }
  } catch {}
  const samples = [];
  const done = new Set();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n && samples.length < max; n = walker.nextNode()) {
    const e = n.parentElement;
    if (!e || done.has(e)) continue;
    done.add(e);
    if (!base && e.hasAttribute(SEEN)) continue;
    if (e.closest("svg, script, style, noscript, template, option, [disabled], [aria-disabled='true']")) continue;
    const text = own(e);
    if (!/[\p{L}\p{N}]/u.test(text) || !vis(e)) continue;
    const cs = getComputedStyle(e);
    const fill = cs.webkitTextFillColor;
    let loops = false;
    for (let a = e; a && !loops; a = a.parentElement) loops = looping.has(a);
    const uncertain = loops || cs.backgroundClip === "text" || (fill && fill !== cs.color && rgba(fill) && rgba(fill)[3] === 0);
    const fg = rgba(cs.color);
    if (!fg) continue;
    const chain = [];
    for (let a = e; a; a = a.parentElement) {
      const as = a === e ? cs : getComputedStyle(a);
      const bg = rgba(as.backgroundColor) || [0, 0, 0, 0];
      const op = parseFloat(as.opacity);
      const img = as.backgroundImage && as.backgroundImage !== "none";
      if (bg[3] > 0 || op < 1 || img) chain.push({ bg, op: Number.isFinite(op) ? op : 1, ...(img ? { img: true } : {}) });
      // no early stop: an ancestor's opacity fades an opaque card as well
    }
    samples.push({ text: text.slice(0, 40), fg, chain, fontPx: parseFloat(cs.fontSize) || 0, weight: parseInt(cs.fontWeight, 10) || 400, ...(uncertain ? { uncertain: true } : {}) });
  }
  return samples;
}

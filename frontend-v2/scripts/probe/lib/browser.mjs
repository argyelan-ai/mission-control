// In-page measurement code for the UI probe. Every exported function here is
// passed to page.evaluate(), so it must be self-contained (no outer scope).

/**
 * Tag every visible opener candidate with data-probe-c and describe it.
 * `within` (optional CSS selector) limits collection to one subtree.
 */
export function collectCandidates(args) {
  const { within, ariaOnly } = args || {};
  const SEL = ariaOnly
    ? '[role=tab], [aria-expanded="false"], summary'
    : 'button, select, [role=combobox], [aria-haspopup]:not([aria-haspopup="false"]), [aria-expanded="false"], summary, [role=tab]';
  const root = within ? document.querySelector(within) : document;
  if (!root) return [];
  const vis = (e) => {
    const r = e.getBoundingClientRect();
    const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none" && +s.opacity !== 0;
  };
  const labelOf = (e) =>
    (e.getAttribute("aria-label") || e.innerText || e.getAttribute("title") || e.getAttribute("placeholder") || e.getAttribute("name") || "")
      .trim()
      .replace(/\s+/g, " ")
      .slice(0, 60);
  const els = [...new Set(root.querySelectorAll(SEL))].filter(vis);
  const attr = ariaOnly ? "data-probe-n" : "data-probe-c";
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
      type: e.getAttribute("type"),
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

/** Mark every element currently in the DOM as "seen" (baseline for a click). */
export function markSeen() {
  document.querySelectorAll("*").forEach((e) => e.setAttribute("data-probe-seen", "1"));
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
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const vis = (e) => {
    const r = e.getBoundingClientRect();
    const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none" && +s.opacity !== 0 && e.getAttribute("data-state") !== "closed";
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
    const roots = [...document.querySelectorAll("body *:not([data-probe-seen])")].filter((e) => {
      if (!e.parentElement || !e.parentElement.hasAttribute("data-probe-seen") && e.parentElement !== document.body) return false;
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
          const inside = !!t && (!t.hasAttribute("data-probe-seen") || e.contains(t));
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
  };
}

/** Count still-visible new layer roots (used after Escape to see if it closed). */
export function countNewLayers() {
  const vis = (e) => {
    const r = e.getBoundingClientRect();
    const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none" && +s.opacity !== 0;
  };
  return [...document.querySelectorAll("body *:not([data-probe-seen])")].filter((e) => {
    const p = e.parentElement;
    if (!p || (!p.hasAttribute("data-probe-seen") && p !== document.body)) return false;
    if (!vis(e)) return false;
    const r = e.getBoundingClientRect();
    return r.width * r.height >= 400;
  }).length;
}

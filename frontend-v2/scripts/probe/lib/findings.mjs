// Finding heuristics for the UI probe — pure functions over measurements the
// browser side collected. Kept free of Playwright so they can be unit-tested.

export const MIN_TARGET_PX = 44; // WCAG 2.5.5 / Apple HIG touch target
export const MOBILE_MAX_WIDTH = 500; // widths at or below count as "phone"

/**
 * Which sides of a rect stick out of the viewport.
 * @param {{left:number, top:number, right:number, bottom:number}} rect
 * @param {{w:number, h:number}} vp
 */
export function offViewportSides(rect, vp, tol = 1) {
  const sides = [];
  if (rect.left < -tol) sides.push("left");
  if (rect.top < -tol) sides.push("top");
  if (rect.right > vp.w + tol) sides.push("right");
  if (rect.bottom > vp.h + tol) sides.push("bottom");
  return sides;
}

/**
 * elementFromPoint probe: each sample says whether the point is on screen and
 * whether the topmost element there belongs to the layer. A layer is "covered"
 * when at least `min` on-screen points hit something else.
 * @param {Array<{inViewport:boolean, inside:boolean}>} samples
 */
export function coveredVerdict(samples, min = 2) {
  const onScreen = samples.filter((s) => s.inViewport);
  const covered = onScreen.filter((s) => !s.inside).length;
  return { covered, onScreen: onScreen.length, flagged: covered >= min };
}

/** Horizontal page overflow in px (0 when none). */
export function horizontalOverflow(scrollWidth, viewportWidth, tol = 1) {
  const d = Math.round(scrollWidth - viewportWidth);
  return d > tol ? d : 0;
}

/**
 * Touch targets under the minimum, only judged on phone widths.
 * @param {Array<{label:string, w:number, h:number}>} targets
 */
export function smallTargets(targets, viewportWidth, min = MIN_TARGET_PX) {
  if (viewportWidth > MOBILE_MAX_WIDTH) return [];
  return targets.filter((t) => t.w > 0 && t.h > 0 && (t.w < min || t.h < min));
}

/** A layer that opened but shows nothing: no text, no media, no controls. */
export function isEmptyLayer(layer) {
  return (layer.text || "").trim().length === 0 && !layer.media && !layer.interactive;
}

/** A page whose main area has (almost) no content after load. */
export function isEmptyPage(mainTextLength, mainInteractive, min = 20) {
  return mainTextLength < min && mainInteractive === 0;
}

/**
 * Console noise caused by the probe's own write lock (aborted requests,
 * refused WebSockets) — not a finding about the UI.
 */
export function isProbeNoise(msg) {
  return /net::ERR_FAILED|net::ERR_ABORTED|net::ERR_BLOCKED|WebSocket connection to .* failed|WebSocket is closed/i.test(String(msg));
}

const SEV = { high: 3, medium: 2, low: 1 };

/**
 * Turn one measured state into findings.
 * @param {object} state  see browser.mjs measureState()
 * @param {{isBase?: boolean}} opts  base state = page just loaded (page-level checks)
 */
export function evaluateState(state, opts = {}) {
  const vp = state.viewport;
  const f = [];
  for (const layer of state.layers || []) {
    if (layer.backdrop) continue; // scrim behind a dialog: nothing to judge
    // Inline expansions may run below the fold (the page scrolls); only
    // floating layers must fit vertically.
    const sides = offViewportSides(layer.rect, vp).filter((x) => layer.kind !== "inline" || x === "left" || x === "right");
    if (sides.length) {
      f.push({ type: "offscreen", severity: "high", message: `${layer.kind} sticks out of the viewport (${sides.join(", ")})`, detail: { rect: layer.rect, label: layer.label } });
    }
    const cov = coveredVerdict(layer.samples || []);
    if (cov.flagged) {
      f.push({ type: "covered", severity: "high", message: `${layer.kind} is behind other elements (${cov.covered}/${cov.onScreen} probe points hit something else)`, detail: { label: layer.label, hits: layer.hitBy || [] } });
    }
    if (layer.clippedBy) {
      f.push({ type: "clipped", severity: "high", message: `${layer.kind} is cut off by its container ${layer.clippedBy}`, detail: { label: layer.label, rect: layer.rect } });
    }
    if (isEmptyLayer(layer)) {
      f.push({ type: "empty", severity: "medium", message: `${layer.kind} opened but is empty`, detail: { label: layer.label } });
    }
    if (layer.clippedText > 0) {
      f.push({ type: "clipped-text", severity: "medium", message: `${layer.clippedText} text element(s) cut off without ellipsis inside ${layer.kind}`, detail: { examples: layer.clippedExamples || [] } });
    }
    // Small targets are judged in floating layers only; inline "layers" are
    // often just re-rendered page rows that the base state already counted.
    const small = layer.kind === "inline" ? [] : smallTargets(layer.targets || [], vp.w);
    if (small.length) {
      f.push({ type: "small-target", severity: "low", message: `${small.length} control(s) under ${MIN_TARGET_PX}px inside ${layer.kind}`, detail: { examples: small.slice(0, 6).map((t) => `${t.label || "(no label)"} ${Math.round(t.w)}x${Math.round(t.h)}`) } });
    }
  }
  const ovf = horizontalOverflow(state.scrollWidth, vp.w);
  if (ovf) f.push({ type: "h-scroll", severity: "high", message: `page scrolls sideways by ${ovf}px`, detail: { offenders: state.overflowOffenders || [] } });
  if (opts.isBase) {
    const small = smallTargets(state.targets || [], vp.w);
    if (small.length) {
      f.push({ type: "small-target", severity: "low", message: `${small.length} of ${(state.targets || []).length} controls under ${MIN_TARGET_PX}px`, detail: { examples: small.slice(0, 8).map((t) => `${t.label || "(no label)"} ${Math.round(t.w)}x${Math.round(t.h)}`) } });
    }
    if (isEmptyPage(state.mainTextLength ?? 0, state.mainInteractive ?? 0)) {
      f.push({ type: "empty", severity: "medium", message: "page shows no content after load", detail: {} });
    }
  }
  if (state.escClosed === false) {
    f.push({ type: "esc", severity: "medium", message: "floating layer does not close on Escape", detail: {} });
  } else if (state.escClosed === "synthetic-only") {
    // Seen live: the Escape handler exists, but a re-render during the real
    // key event re-subscribes it, so the press is lost. Users are affected.
    f.push({ type: "esc", severity: "medium", message: "floating layer ignores a real Escape key press (a synthetic window event closes it: handler exists but misses the real event)", detail: {} });
  }
  for (const e of (state.consoleErrors || []).filter((m) => !isProbeNoise(m))) {
    f.push({ type: "console-error", severity: "medium", message: e.slice(0, 240), detail: {} });
  }
  return f;
}

/** Sort findings most severe first, stable otherwise. */
export function rankFindings(list) {
  return list
    .map((x, i) => ({ x, i }))
    .sort((a, b) => (SEV[b.x.severity] || 0) - (SEV[a.x.severity] || 0) || a.i - b.i)
    .map(({ x }) => x);
}

/** Collapse identical findings (same type + message) across states, keep a count. */
export function dedupeFindings(list) {
  const map = new Map();
  for (const x of list) {
    const k = `${x.page}|${x.width}|${x.type}|${x.message}`;
    const hit = map.get(k);
    if (hit) {
      hit.count += 1;
      if (x.shot && hit.shots.length < 5) hit.shots.push(x.shot);
    } else {
      map.set(k, { ...x, count: 1, shots: x.shot ? [x.shot] : [] });
    }
  }
  return [...map.values()];
}

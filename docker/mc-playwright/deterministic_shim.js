// Deterministic page-clock shim for /record (2026-07-15).
//
// Root cause it fixes: headless Chromium without a GPU renders heavy canvas/
// particle animations at real-time-observed ~11fps instead of the page's
// intended 60fps (Playwright's record_video captures the video in real
// time), so the delivered bench video looks like slow motion + stutter even
// though the code itself runs fine. Proven fix: virtualize the page's time
// sources (performance.now/Date.now/rAF/setTimeout/setInterval) so the
// *page* believes exactly 1000/30 ms elapsed per captured frame, no matter
// how long Chromium actually took to render it. The caller (service.py)
// then drives the page one virtual frame at a time via window.__mcTick(ms)
// and screenshots each settled frame via CDP Page.captureScreenshot — the
// output is a perfectly smooth 30fps video regardless of render slowness.
//
// Installed via page.add_init_script() so it runs before any of the
// target page's own scripts observe the real clock.
(() => {
  let vt = 0;
  const rafQ = [];
  let nextRaf = 1;
  const timers = [];
  let nextTimer = 1;
  const realDateNow = Date.now.bind(Date);
  const epoch = realDateNow();
  performance.now = () => vt;
  Date.now = () => epoch + vt;
  const RealDate = Date;
  window.Date = class extends RealDate {
    constructor(...a){ a.length ? super(...a) : super(epoch + vt); }
    static now(){ return epoch + vt; }
  };
  window.requestAnimationFrame = (fn) => { const id = nextRaf++; rafQ.push({id, fn}); return id; };
  window.cancelAnimationFrame = (id) => { const i = rafQ.findIndex(r => r.id === id); if (i>=0) rafQ.splice(i,1); };
  window.setTimeout = (fn, d = 0, ...args) => { const id = nextTimer++; timers.push({id, at: vt + Math.max(0,d), fn, args, interval: 0}); return id; };
  window.setInterval = (fn, d = 0, ...args) => { const id = nextTimer++; timers.push({id, at: vt + Math.max(1,d), fn, args, interval: Math.max(1,d)}); return id; };
  window.clearTimeout = window.clearInterval = (id) => { const i = timers.findIndex(t => t.id === id); if (i>=0) timers.splice(i,1); };
  // Advances the virtual clock by stepMs, running every timer/interval due
  // in between (in order), settling CSS animations to the new virtual time,
  // then flushing the rAF queue. Called once per captured frame.
  window.__mcTick = (stepMs) => {
    const target = vt + stepMs;
    for (;;) {
      const due = timers.filter(t => t.at <= target).sort((a,b) => a.at - b.at)[0];
      if (!due) break;
      vt = due.at;
      if (due.interval) due.at += due.interval; else timers.splice(timers.indexOf(due), 1);
      try {
        // setTimeout/setInterval technically allow a string (eval'd) as the
        // handler — extremely rare in practice, but guard rather than throw.
        if (typeof due.fn === "function") due.fn(...due.args);
      } catch (e) {}
    }
    vt = target;
    try { document.getAnimations().forEach(a => { try { a.currentTime = vt; } catch(e){} }); } catch(e) {}
    const q = rafQ.splice(0, rafQ.length);
    for (const r of q) { try { r.fn(vt); } catch (e) {} }
  };
})();

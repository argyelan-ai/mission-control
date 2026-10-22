// /record network guard (W0.9) — installed via page.add_init_script() into
// every frame of the recorded page, before any page code runs.
//
// Browser-context routing blocks HTTP(S), file and WebSocket requests, and a
// dead proxy backs it up. WebRTC is the one channel neither covers (its UDP
// traffic does not go through routing or an HTTP proxy), so its constructors
// are replaced by stubs that throw. Every attempt is counted in
// window.__mcNetBlocked, which the sidecar reads after the capture and adds
// to network_attempts.
(() => {
  const w = window;
  if (w.__mcNetGuardInstalled) return;
  Object.defineProperty(w, "__mcNetGuardInstalled", { value: true });
  w.__mcNetBlocked = 0;

  const blocked = (name) =>
    function () {
      w.__mcNetBlocked += 1;
      throw new DOMException(`${name} is disabled in the benchmark recorder`, "NotAllowedError");
    };

  for (const name of ["RTCPeerConnection", "webkitRTCPeerConnection", "RTCDataChannel"]) {
    if (name in w) {
      try {
        Object.defineProperty(w, name, { value: blocked(name), writable: false, configurable: false });
      } catch (_) {
        /* already locked by the browser — routing + proxy still apply */
      }
    }
  }
})();

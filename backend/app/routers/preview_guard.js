(function (w, d) {
  /* MC preview guard. Runs first in every previewed agent page (the srcdoc
     guard head in frontend HtmlPreview.tsx, and the folder pages served by
     routers/deliverable_view.py). The page sits in an opaque origin (sandbox
     without allow-same-origin): reading localStorage, sessionStorage or
     document.cookie throws a SecurityError there, and a game that reads its
     high score on the first line of its script dies before it binds a single
     click. Stand-ins that live in memory only keep such pages alive; nothing
     is stored anywhere, and the sandbox stays as it is. One copy of this file
     lives in the frontend (previewGuard.ts) - a test keeps the two equal. */
  function memoryStorage() {
    var data = Object.create(null);
    var api = {
      getItem: function (k) { k = String(k); return k in data ? data[k] : null; },
      setItem: function (k, v) { data[String(k)] = String(v); },
      removeItem: function (k) { delete data[String(k)]; },
      clear: function () { data = Object.create(null); },
      key: function (i) { var ks = Object.keys(data); return i >= 0 && i < ks.length ? ks[i] : null; }
    };
    var own = function (p) { return typeof p === "string" && !(p in api) && p !== "length"; };
    if (typeof Proxy !== "function") {
      Object.defineProperty(api, "length", { get: function () { return Object.keys(data).length; } });
      return api;
    }
    /* storage.highscore = 3 and storage["x"] work like on a real Storage. */
    return new Proxy(api, {
      get: function (t, p) {
        if (p === "length") return Object.keys(data).length;
        if (own(p)) return p in data ? data[p] : undefined;
        return t[p];
      },
      set: function (t, p, v) { if (own(p)) data[p] = String(v); return true; },
      has: function (t, p) { return p in t || (own(p) && p in data); },
      deleteProperty: function (t, p) { if (own(p)) delete data[p]; return true; },
      ownKeys: function () { return Object.keys(data); },
      getOwnPropertyDescriptor: function (t, p) {
        if (own(p) && p in data) return { value: data[p], writable: true, enumerable: true, configurable: true };
        return undefined;
      }
    });
  }
  ["localStorage", "sessionStorage"].forEach(function (name) {
    try { var s = w[name]; if (s && typeof s.getItem === "function") return; } catch (e) { /* opaque origin */ }
    try { Object.defineProperty(w, name, { value: memoryStorage(), configurable: true, enumerable: true }); } catch (e) { /* keep the original */ }
  });
  /* The cookie always gets the jar: Chromium throws on reading it here,
     WebKit gives "" and silently drops every write - a page that keeps its
     state in a cookie lost it on an iPhone without any error. */
  var jar = Object.create(null);
  try {
    Object.defineProperty(d, "cookie", {
      configurable: true,
      get: function () { return Object.keys(jar).map(function (k) { return k + "=" + jar[k]; }).join("; "); },
      set: function (v) {
        var text = String(v);
        var pair = text.split(";")[0];
        var at = pair.indexOf("=");
        var key = at < 0 ? "" : pair.slice(0, at).trim();
        if (/;\s*max-age\s*=\s*(0|-\d+)\s*(;|$)/i.test(text)) delete jar[key];
        else jar[key] = (at < 0 ? pair : pair.slice(at + 1)).trim();
      }
    });
  } catch (e) { /* keep the original */ }
  /* A page that still breaks: tell MC once, so it can show a hint outside
     the frame. A fixed word, never the page's text. Only thrown errors
     count - a rejected promise is no broken page: a start sound before the
     first tap (NotAllowedError) or a fetch (connect-src 'none') reject in
     pages that run fine. */
  var told = false;
  function tell() {
    if (told) return;
    told = true;
    try { w.parent.postMessage({ mcPreview: "page-error" }, "*"); } catch (e) { /* no parent */ }
  }
  w.addEventListener("error", tell);
})(window, document);

// Tests for the review round: token hygiene, loading detection, sampling of
// repeated components, deeper coverage and honest report numbers.
import { describe, expect, it } from "vitest";
import { clickVerdict, dangerousLabel, isCloser, nestedVerdict, redactSecrets } from "../lib/guard.mjs";
import { dedupeFindings, evaluateState, isLoadingText } from "../lib/findings.mjs";
import { familyKey, parseArgs, renderMarkdown, sampleRepeats } from "../lib/report.mjs";
import { resolveRoutes } from "../lib/routes.mjs";
import { collectCandidates } from "../lib/browser.mjs";
import { tokenInitScript } from "../lib/context.mjs";

describe("redactSecrets", () => {
  it("removes token query values, bearer headers, JWT-like strings and the literal token", () => {
    const tok = "abc.def.ghi-secret";
    const msg = `GET http://h/api/v1/stream?token=${tok}&x=1 failed; Authorization: Bearer ${tok}; eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig and raw ${tok}`;
    const out = redactSecrets(msg, [tok]);
    expect(out).not.toContain(tok);
    expect(out).not.toMatch(/eyJ/);
    expect(out).toContain("token=***&x=1");
    expect(out).toContain("Bearer ***");
  });
  it("scrubs a token query value even when the value is not a known secret", () => {
    expect(redactSecrets("WebSocket ws://h/ws/terminal?token=zz9-other&cols=80 failed", [])).toBe("WebSocket ws://h/ws/terminal?token=***&cols=80 failed");
    expect(redactSecrets("GET /api/x?access_token=q1w2e3", [])).toBe("GET /api/x?access_token=***");
  });
  it("leaves harmless text alone and tolerates empty secrets", () => {
    expect(redactSecrets("TypeError: x is undefined", ["", null])).toBe("TypeError: x is undefined");
  });
});

describe("clickVerdict submenus", () => {
  it("clicks a menu item only when it opens a submenu", () => {
    expect(clickVerdict({ label: "Sort by", role: "menuitem" }).ok).toBe(false);
    expect(clickVerdict({ label: "Sort by", role: "menuitem", haspopup: "menu" }).ok).toBe(true);
    expect(clickVerdict({ label: "Delete", role: "menuitem", haspopup: "menu" }).ok).toBe(false);
  });
});

describe("tokenInitScript", () => {
  it("writes the token only on the app's own origin", () => {
    const store = {};
    const fakeLs = { setItem: (k, v) => (store[k] = v) };
    tokenInitScript({ token: "T", origin: "http://other.example", _loc: { origin: "http://app.example" }, _ls: fakeLs });
    expect(store).toEqual({});
    tokenInitScript({ token: "T", origin: "http://app.example", _loc: { origin: "http://app.example" }, _ls: fakeLs });
    expect(store).toEqual({ mc_auth_token: "T" });
  });
});

describe("collectCandidates (DOM)", () => {
  const withBoxes = (fn) => {
    // jsdom has no layout and no innerText: give every element a box and
    // let innerText fall back to textContent.
    const orig = Element.prototype.getBoundingClientRect;
    const origText = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "innerText");
    Element.prototype.getBoundingClientRect = () => ({ width: 40, height: 40, left: 0, top: 0, right: 40, bottom: 40 });
    Object.defineProperty(HTMLElement.prototype, "innerText", { configurable: true, get() { return this.textContent; } });
    try {
      return fn();
    } finally {
      Element.prototype.getBoundingClientRect = orig;
      if (origText) Object.defineProperty(HTMLElement.prototype, "innerText", origText);
      else delete HTMLElement.prototype.innerText;
    }
  };
  it("sees a <button> without type inside a form as a submit button", () => {
    document.body.innerHTML = `<main><form><button>Filter</button><button type="button">Open</button></form><button>Outside</button></main>`;
    const c = withBoxes(() => collectCandidates({}));
    expect(c.map((x) => [x.label, x.type])).toEqual([["Filter", "submit"], ["Open", "button"], ["Outside", "button"]]);
    expect(clickVerdict(c[0])).toMatchObject({ ok: false, reason: "guarded:submit" });
  });
  it("collects submenu menu items and reports aria-haspopup", () => {
    document.body.innerHTML = `<main><div role="menu"><div role="menuitem" aria-haspopup="menu">Sort by</div><div role="menuitem">Plain</div></div></main>`;
    const c = withBoxes(() => collectCandidates({}));
    expect(c.map((x) => x.label)).toContain("Sort by");
    expect(c.find((x) => x.label === "Sort by").haspopup).toBe("menu");
  });
  it("onlyNew skips elements that were already on the page before the parent opened", () => {
    document.body.innerHTML = `<main><button data-probe-seen="1">Old</button><section><button aria-haspopup="listbox">New select</button></section></main>`;
    const c = withBoxes(() => collectCandidates({ onlyNew: true, attr: "data-probe-n" }));
    expect(c.map((x) => x.label)).toEqual(["New select"]);
    expect(document.querySelector("[data-probe-n]").textContent).toBe("New select");
  });
});

describe("loading detection", () => {
  it.each(["Loading runtimes...", "Loading…", "Loading recipes …", "Timeline wird geladen…", "lädt…"])("treats %s as a loading hint", (s) => {
    expect(isLoadingText(s)).toBe(true);
  });
  it.each(["Loading the weights takes a few minutes on the first start. The runtime card shows the live status.", "Downloads", "Diff konnte nicht geladen werden."])(
    "does not treat %s as loading",
    (s) => {
      expect(isLoadingText(s)).toBe(false);
    },
  );
  it("reports a page that is still loading after the wait", () => {
    const f = evaluateState({ viewport: { w: 1440, h: 900 }, scrollWidth: 1440, layers: [], stillLoading: ["Loading runtimes..."], mainTextLength: 99, mainInteractive: 3 }, { isBase: true });
    expect(f).toEqual([expect.objectContaining({ type: "loading", severity: "high" })]);
    expect(f[0].message).toMatch(/Loading runtimes/);
  });
});

describe("escape findings on phones", () => {
  it("are not reported at phone width (no hardware Escape key)", () => {
    const st = (w) => ({ viewport: { w, h: 800 }, scrollWidth: w, layers: [], escClosed: false });
    expect(evaluateState(st(390))).toEqual([]);
    expect(evaluateState(st(1440)).map((x) => x.type)).toEqual(["esc"]);
  });
});

describe("dedupeFindings keeps every opener", () => {
  it("lists all openers instead of attributing repeats to the first one", () => {
    const f = (opener) => ({ page: "/agents", width: 1440, type: "esc", severity: "medium", message: "m", opener, shot: `${opener}.png` });
    const d = dedupeFindings([f("New agent"), f("Actions: A"), f("Actions: B"), f("Actions: A")]);
    expect(d).toHaveLength(1);
    expect(d[0]).toMatchObject({ count: 4, openers: ["New agent", "Actions: A", "Actions: B"] });
    const md = renderMarkdown({ base: "b", startedAt: "s", finishedAt: "f", widths: [1440], skippedRoutes: [], writes: { blocked: 0, passed: 0, websocketsRefused: 0, samples: [] }, pages: [{ path: "/agents", width: 1440, counts: {}, findings: d, states: [] }] });
    expect(md).toContain('after opening "New agent", "Actions: A", "Actions: B"');
  });
});

describe("sampling repeated components", () => {
  it("familyKey folds per-item labels into one family", () => {
    expect(familyKey({ tag: "button", role: null, label: "Actions: Alpha" })).toBe(familyKey({ tag: "button", role: null, label: "Actions: Beta" }));
    expect(familyKey({ tag: "button", role: null, label: "Reference files" })).toBe("button||Reference files");
    expect(familyKey({ tag: "button", role: null, label: "Task 12 menu" })).toBe(familyKey({ tag: "button", role: null, label: "Task 7 menu" }));
    expect(familyKey({ tag: "button", role: null, label: "Filter" })).not.toBe(familyKey({ tag: "button", role: null, label: "Sort" }));
  });
  it("keeps the first and last of a family, drops the middle, leaves singles and tabs alone", () => {
    const c = (label, role = null) => ({ tag: "button", role, label });
    const list = [c("Filter"), c("Actions: A"), c("Actions: B"), c("Actions: C"), c("Actions: D"), c("Overview", "tab"), c("Logs", "tab"), c("Files", "tab")];
    const { keep, skipped } = sampleRepeats(list);
    expect(keep.map((x) => x.label)).toEqual(["Filter", "Actions: A", "Actions: D", "Overview", "Logs", "Files"]);
    expect(skipped.map((x) => x.label)).toEqual(["Actions: B", "Actions: C"]);
  });
});

describe("new CLI flags", () => {
  it("parses --all-repeats, --nested-max and --load-timeout", () => {
    const o = parseArgs(["--all-repeats", "--nested-max", "5", "--load-timeout", "9000"]);
    expect(o).toMatchObject({ allRepeats: true, nestedMax: 5, loadTimeoutMs: 9000 });
    expect(parseArgs([])).toMatchObject({ allRepeats: false, nestedMax: 20, loadTimeoutMs: 20000 });
  });
});

describe("report numbers", () => {
  it("counts native selects that were only read separately from opened states", () => {
    const md = renderMarkdown({ base: "b", startedAt: "s", finishedAt: "f", widths: [1440], skippedRoutes: [], writes: { blocked: 0, passed: 0, websocketsRefused: 0, samples: [] }, pages: [{ path: "/settings", width: 1440, counts: { opened: 14, candidates: 17, read: 2, sampledOut: 3, guarded: 0, noChange: 1, navigated: 0, failed: 0, nestedOpened: 30, loading: false }, findings: [], states: [] }] });
    expect(md).toContain("| Read (select) |");
    expect(md).toContain("| `/settings` | 1440 | 14 | 17 | 82% | 2 | 30 | 3 |");
  });
});

describe("deep-link views", () => {
  it("resolves a chained id source into a query view (task detail)", async () => {
    const getJson = async (ep) => {
      if (ep === "/api/v1/boards") return [{ id: "b1" }];
      if (ep === "/api/v1/boards/b1/tasks") return [{ id: "t9" }];
      throw new Error(`unexpected ${ep}`);
    };
    const { resolved, skipped } = await resolveRoutes([{ pattern: "/tasks?task=[id]", dynamic: true }], getJson, {
      "/tasks?task=[id]": { chain: [{ endpoint: "/api/v1/boards" }, { endpoint: "/api/v1/boards/{id}/tasks" }] },
    });
    expect(skipped).toEqual([]);
    expect(resolved).toEqual([{ pattern: "/tasks?task=[id]", path: "/tasks?task=t9" }]);
  });
});

describe("isCloser", () => {
  it.each(["Close", "close dialog", "Schliessen", "Schließen", "Back", "Zurück", "×", "✕"])("skips %s on the second level", (l) => {
    expect(isCloser(l)).toBe(true);
  });
  it.each(["Closed tasks", "Background", "Filter", ""])("does not skip %s", (l) => {
    expect(isCloser(l)).toBe(false);
  });
});

describe("never-click list additions from the first live run", () => {
  it.each([["Test embeddings", "test"], ["Analyze Now", "analyze"], ["Re-probe", "re-probe"], ["Refetch", "refetch"], ["Testen", "testen"], ["Analysieren", "analysieren"]])("blocks %s", (l, stem) => {
    expect(dangerousLabel(l)).toBe(stem);
  });
  it.each(["Latest", "Tester", "Contest"])("does not block %s", (l) => {
    expect(dangerousLabel(l)).toBeNull();
  });
});

describe("views opened by a click that are still loading", () => {
  it("are reported too (medium), not only the page as loaded", () => {
    const f = evaluateState({ viewport: { w: 1440, h: 900 }, scrollWidth: 1440, layers: [], stillLoading: ["Loading catalog..."] });
    expect(f).toEqual([expect.objectContaining({ type: "loading", severity: "medium" })]);
  });
});

describe("nestedVerdict: a dialog's own confirm button is never clicked", () => {
  it("guards create-style labels inside a floating parent only", () => {
    expect(nestedVerdict({ label: "Create loop" }, true)).toMatchObject({ ok: false, reason: "guarded:confirm-in-dialog" });
    expect(nestedVerdict({ label: "Erstellen" }, true).ok).toBe(false);
    expect(nestedVerdict({ label: "Create loop" }, false).ok).toBe(true);
    expect(nestedVerdict({ label: "New board" }, true).ok).toBe(true);
    expect(nestedVerdict({ label: "Delete" }, false).ok).toBe(false);
    expect(nestedVerdict({ label: "Create", role: "tab" }, true).ok).toBe(true);
  });
});

import { describe, expect, it } from "vitest";
import { homeAlertsToBanner, pickLocalized, type HomeAlert } from "./homeAlerts";

const base: HomeAlert = {
  id: "feed-quiet",
  severity: "warning",
  title: { de: "Feed still", en: "Feed quiet" },
  detail: null,
  href: "/feeds",
};

describe("pickLocalized", () => {
  it("returns plain strings unchanged", () => {
    expect(pickLocalized("x", "de")).toBe("x");
  });
  it("picks the requested locale", () => {
    expect(pickLocalized({ de: "still", en: "quiet" }, "en")).toBe("quiet");
    expect(pickLocalized({ de: "still", en: "quiet" }, "de")).toBe("still");
  });
  it("falls back to English, then to any value", () => {
    expect(pickLocalized({ en: "quiet" }, "de")).toBe("quiet");
    expect(pickLocalized({ fr: "calme" }, "de")).toBe("calme");
  });
  it("returns empty string for null", () => {
    expect(pickLocalized(null, "de")).toBe("");
  });
});

describe("homeAlertsToBanner", () => {
  it("maps severity to color and keeps href", () => {
    const [w] = homeAlertsToBanner([base], "de", { warning: "W", error: "E" });
    expect(w).toEqual({ key: "feed-quiet", label: "Feed still", color: "W", href: "/feeds", title: undefined });
    const [c] = homeAlertsToBanner([{ ...base, severity: "critical" }], "en", { warning: "W", error: "E" });
    expect(c.color).toBe("E");
    expect(c.label).toBe("Feed quiet");
  });
  it("puts the detail into the hover title", () => {
    const [a] = homeAlertsToBanner([{ ...base, detail: { de: "seit 36 Tagen", en: "36 days" } }], "de", { warning: "W", error: "E" });
    expect(a.title).toBe("seit 36 Tagen");
  });
  it("returns an empty list for undefined input", () => {
    expect(homeAlertsToBanner(undefined, "de", { warning: "W", error: "E" })).toEqual([]);
  });
});

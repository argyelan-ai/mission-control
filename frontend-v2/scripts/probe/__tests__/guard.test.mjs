import { describe, expect, it } from "vitest";
import { clickVerdict, dangerousLabel, isWriteMethod, redactUrl } from "../lib/guard.mjs";

describe("isWriteMethod", () => {
  it("lets only GET/HEAD/OPTIONS through", () => {
    for (const m of ["GET", "get", "HEAD", "OPTIONS"]) expect(isWriteMethod(m)).toBe(false);
    for (const m of ["POST", "PUT", "PATCH", "DELETE", "post", "PROPFIND", "", undefined]) expect(isWriteMethod(m)).toBe(true);
  });
});

describe("dangerousLabel", () => {
  it.each([
    ["Delete task", "delete"],
    ["Stop agent", "stop"],
    ["Restarting…", "restart"],
    ["Cancel task", "cancel"],
    ["Dispatch", "dispatch"],
    ["Approve", "approve"],
    ["Deploy now", "deploy"],
    ["Switch model", "switch"],
    ["Save changes", "save"],
    ["Send", "send"],
    ["Submit", "submit"],
    ["Log out", "log out"],
    ["Aufgabe löschen", "löschen"],
    ["Speichern", "speichern"],
    ["Neu starten", "neu starten"],
    ["Abbrechen", "abbrechen"],
    ["Freigeben", "freigeben"],
  ])("blocks %s", (label, stem) => {
    expect(dangerousLabel(label)).toBe(stem);
  });

  it.each(["Runtimes", "Desktop", "Filter", "More options", "Board switcher", "Sender", "New task", "Add agent", "Status", "Mehr", ""])(
    "does not block %s",
    (label) => {
      expect(dangerousLabel(label)).toBeNull();
    },
  );
});

describe("clickVerdict", () => {
  it("never clicks submit buttons, switches or disabled controls", () => {
    expect(clickVerdict({ label: "Filter", type: "submit" }).ok).toBe(false);
    expect(clickVerdict({ label: "Dark mode", role: "switch" }).ok).toBe(false);
    expect(clickVerdict({ label: "Filter", disabled: true })).toEqual({ ok: false, reason: "disabled" });
  });
  it("exempts tabs (they only change the visible view)", () => {
    expect(clickVerdict({ label: "Run record", role: "tab" }).ok).toBe(true);
    expect(clickVerdict({ label: "Run record" }).ok).toBe(false);
  });
  it("allows plain openers", () => {
    expect(clickVerdict({ label: "More options", tag: "button" })).toEqual({ ok: true });
  });
});

describe("redactUrl", () => {
  it("drops query and fragment so tokens never reach a log", () => {
    expect(redactUrl("http://localhost/api/v1/stream?token=secret#x")).toBe("http://localhost/api/v1/stream");
    expect(redactUrl("/api/x?token=s")).toBe("/api/x");
  });
});

/**
 * First-run routing (lib/firstRun.ts) — finding 17.10.2026: after the first
 * registration a reload or a later login landed on an empty Home instead of
 * the setup wizard.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { api } from "@/lib/api";
import {
  _resetFirstRunCheckForTests,
  markWizardDone,
  postLoginPath,
  shouldRedirectToSetup,
} from "@/lib/firstRun";

beforeEach(() => {
  vi.restoreAllMocks();
  _resetFirstRunCheckForTests();
});

describe("postLoginPath", () => {
  it("sends the first registration to /setup without asking", async () => {
    const firstRun = vi.spyOn(api.auth, "firstRun");
    expect(await postLoginPath("register")).toBe("/setup");
    expect(firstRun).not.toHaveBeenCalled();
  });

  it("sends a login to /setup while the wizard is pending", async () => {
    vi.spyOn(api.auth, "firstRun").mockResolvedValue({ wizard_pending: true });
    expect(await postLoginPath("login")).toBe("/setup");
  });

  it("sends a login to Home once the wizard is done", async () => {
    vi.spyOn(api.auth, "firstRun").mockResolvedValue({ wizard_pending: false });
    expect(await postLoginPath("login")).toBe("/");
  });

  it("never locks anyone out: a failed check goes to Home", async () => {
    vi.spyOn(api.auth, "firstRun").mockRejectedValue(new Error("API 500"));
    expect(await postLoginPath("login")).toBe("/");
  });
});

describe("shouldRedirectToSetup", () => {
  it("answers once per page load, then leaves in-app navigation alone", async () => {
    const firstRun = vi.spyOn(api.auth, "firstRun").mockResolvedValue({ wizard_pending: true });
    expect(await shouldRedirectToSetup()).toBe(true);
    expect(await shouldRedirectToSetup()).toBe(false);
    expect(firstRun).toHaveBeenCalledTimes(1);
  });

  it("does not redirect when nothing is pending", async () => {
    vi.spyOn(api.auth, "firstRun").mockResolvedValue({ wizard_pending: false });
    expect(await shouldRedirectToSetup()).toBe(false);
  });
});

describe("markWizardDone", () => {
  it("tells the backend and swallows a failure", async () => {
    const done = vi.spyOn(api.auth, "completeFirstRun").mockRejectedValue(new Error("offline"));
    await expect(markWizardDone()).resolves.toBeUndefined();
    expect(done).toHaveBeenCalledTimes(1);
  });
});

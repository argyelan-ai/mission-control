/**
 * First-run wizard routing.
 *
 * Finding (17.10.2026, fresh install): after registering the first admin the
 * operator landed on Home ("No board selected") instead of /setup. The only
 * way into the wizard was the one redirect right after the register request;
 * a reload, a second device or logging in again went to Home.
 *
 * The backend now keeps the state (GET /auth/first-run, services/first_run.py):
 * pending from the first registration until the wizard's last step, only for
 * admins, and never on installs older than the marker. This module is the one
 * place that turns that answer into a route.
 */
import { api } from "./api";

export const SETUP_PATH = "/setup";

/** True when the signed-in user should be sent to the setup wizard. A failed
 *  check answers false: the wizard is a help, it must never lock anyone out
 *  of the app. */
export async function wizardDue(): Promise<boolean> {
  try {
    const res = await api.auth.firstRun();
    return res.wizard_pending === true;
  } catch {
    return false;
  }
}

/** Where to go right after a successful login or registration. */
export async function postLoginPath(mode: "login" | "register"): Promise<string> {
  // The first registration always starts the wizard (the backend marks it
  // pending in the same request) — no extra round trip needed.
  if (mode === "register") return SETUP_PATH;
  return (await wizardDue()) ? SETUP_PATH : "/";
}

/** Once per page load: an admin who opens any page while the wizard is still
 *  pending (reload, bookmark, second device) is sent to it. Later in-app
 *  navigation is left alone, so a deliberate detour is not fought. */
let checkedThisLoad = false;

export async function shouldRedirectToSetup(): Promise<boolean> {
  if (checkedThisLoad) return false;
  checkedThisLoad = true;
  return wizardDue();
}

/** The wizard is finished (or deliberately left via its last step). */
export async function markWizardDone(): Promise<void> {
  try {
    await api.auth.completeFirstRun();
  } catch {
    // Not fatal: the worst case is one more visit to the wizard next time.
  }
}

/** Test seam: reset the once-per-load flag. */
export function _resetFirstRunCheckForTests(): void {
  checkedThisLoad = false;
}

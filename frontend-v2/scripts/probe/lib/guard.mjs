// Write lock for the UI probe — pure logic, no browser imports.
//
// Two independent belts:
//   1. Network: every request whose method is not GET/HEAD/OPTIONS is aborted
//      (see isWriteMethod) and counted; WebSockets are refused entirely.
//   2. Clicks: controls whose label reads like an action with side effects are
//      never clicked (see dangerousLabel), in English and German.
// Belt 1 alone already keeps the backend untouched; belt 2 keeps the browser
// state sane (no local logout, no half-sent forms) and documents intent.

export const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

/** True when a request with this HTTP method must be blocked. */
export function isWriteMethod(method) {
  return !SAFE_METHODS.has(String(method || "").toUpperCase());
}

/**
 * Words that mark a control as an action with side effects. Matched as whole
 * words plus a short English inflection ("Deleted", "Restarting", "Stopping"
 * match; "Runtimes" or "Desktop" do not).
 * "new" / "add" / "create" are deliberately NOT here: those buttons open the
 * creation dialogs we want to see, and the dialog's own confirm button is never
 * clicked (depth-2 probing only touches pure openers).
 */
export const DANGER_STEMS = [
  // English
  "delete", "remove", "stop", "cancel", "dispatch", "approve", "reject", "deny",
  "deploy", "restart", "reboot", "shutdown", "shut down", "switch", "save", "send",
  "submit", "confirm", "apply", "start", "run", "rerun", "execute", "trigger",
  "kill", "abort", "terminate", "archive", "unarchive", "restore", "purge", "reset",
  "logout", "log out", "sign out", "disconnect", "revoke", "rotate", "regenerate",
  "install", "uninstall", "enable", "disable", "activate", "deactivate", "upload",
  "import", "publish", "merge", "retry", "unblock", "assign", "reassign", "pause",
  "resume", "wake", "snooze", "clear", "empty trash", "autopilot", "ai process",
  "process with ai", "sync", "rescan", "recompute", "promote", "demote", "accept",
  "mark as", "mark done", "mark read", "dismiss all", "pin", "unpin", "move to",
  "transfer", "handoff", "hand off", "escalate", "close task", "done", "finish",
  "launch", "boost", "bind", "unbind", "attach", "detach", "join", "leave",
  "record", "call", "mic", "microphone", "voice", "copy token", "reveal",
  "test connection", "test", "analyze", "analyse", "re-probe", "reprobe", "refetch",
  "download",
  // German
  "löschen", "loeschen", "entfernen", "stoppen", "anhalten", "abbrechen", "verwerfen",
  "senden", "absenden", "speichern", "freigeben", "genehmigen", "ablehnen",
  "zuweisen", "bereitstellen", "neustart", "neu starten", "wechseln", "umschalten",
  "übernehmen", "uebernehmen", "starten", "zurücksetzen", "zuruecksetzen",
  "archivieren", "wiederherstellen", "abmelden", "trennen", "beenden",
  "deaktivieren", "aktivieren", "installieren", "ausführen", "ausfuehren",
  "hochladen", "bestätigen", "bestaetigen", "erledigt", "fertig", "pausieren",
  "fortsetzen", "wecken", "leeren", "verschieben", "anheften", "loslösen",
  "herunterladen", "aufnehmen", "anrufen", "testen", "analysieren",
];

const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
// Word boundaries are spelled out because \b is ASCII-only and would
// mis-handle "ö", "ü" etc. Longest stems first so "log out" wins over "log".
const STEMS_SORTED = [...DANGER_STEMS].sort((a, b) => b.length - a.length);
const DANGER_RE = new RegExp(
  "(^|[^\\p{L}\\p{N}])(" +
    STEMS_SORTED.map(esc).join("|") +
    ")(?:s|es|d|ed|ing|ning|ping|ned|ped|t)?(?=$|[^\\p{L}\\p{N}])",
  "iu",
);

/** Returns the matched stem when the label must never be clicked, else null. */
export function dangerousLabel(label) {
  const text = String(label || "").trim();
  if (!text) return null;
  const m = DANGER_RE.exec(text);
  return m ? m[2].toLowerCase() : null;
}

/**
 * Decide whether a candidate control may be clicked.
 * @param {{label?: string, type?: string|null, role?: string|null, disabled?: boolean}} c
 * @returns {{ok: true} | {ok: false, reason: string}}
 */
export function clickVerdict(c) {
  if (c.disabled) return { ok: false, reason: "disabled" };
  if ((c.type || "").toLowerCase() === "submit") return { ok: false, reason: "guarded:submit" };
  const role = (c.role || "").toLowerCase();
  // A menu item that opens a submenu is an opener, not an action — but its
  // label still goes through the never-click list below.
  const submenu = role === "menuitem" && c.haspopup && c.haspopup !== "false";
  if (!submenu && ["switch", "checkbox", "radio", "menuitem", "option"].includes(role)) {
    return { ok: false, reason: `guarded:role=${c.role}` };
  }
  // Tabs only switch the visible view, they cannot act on data. Exempting
  // them keeps tabs like "Run record" probe-able although "run" is a stem.
  if ((c.role || "").toLowerCase() === "tab") return { ok: true };
  const stem = dangerousLabel(c.label);
  if (stem) return { ok: false, reason: `guarded:${stem}` };
  return { ok: true };
}

/** Strip query + fragment so logged URLs can never carry a token. */
export function redactUrl(url) {
  try {
    const u = new URL(url);
    return `${u.origin}${u.pathname}`;
  } catch {
    return String(url).split(/[?#]/)[0];
  }
}

/**
 * Scrub credentials out of free text (console messages, page errors) before it
 * is stored in probe.json / report.md: `token=` query values, bearer headers,
 * JWT-shaped strings and every literal secret passed in.
 */
export function redactSecrets(text, secrets = []) {
  let out = String(text ?? "");
  for (const s of secrets) {
    if (typeof s === "string" && s.length >= 4) out = out.split(s).join("***");
  }
  return out
    .replace(/([?&](?:access_)?token=)[^&\s'"#)]+/gi, "$1***")
    .replace(/(Bearer\s+)[A-Za-z0-9._~+/=-]+/gi, "$1***")
    .replace(/eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*/g, "***");
}

/**
 * Buttons that only close or step back out of what was just opened. Clicking
 * them on the second level would tear down the parent for nothing.
 */
export function isCloser(label) {
  return /^(close|close dialog|close panel|schliessen|schließen|back|zurück|zurueck|×|✕|x)$/iu.test(String(label || "").trim());
}

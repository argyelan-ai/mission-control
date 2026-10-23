/**
 * Classes for a control that acts on the MC host (the Mac MC runs on) — "Open
 * on the Mac", "Show in Finder". The server opens the file there, not on the
 * device in hand, so on a phone or tablet such a button only does something
 * the operator cannot see (round-1 user test r2 S6: tapped by mistake next to
 * the expand arrow). Shown from `md` with a fine pointer (mouse or trackpad)
 * only; hidden everywhere else.
 */
export const HOST_ONLY_CLASS = "hidden md:pointer-fine:inline-flex";

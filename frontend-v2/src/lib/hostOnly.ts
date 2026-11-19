import { useSyncExternalStore } from "react";

/**
 * Controls that act on the MC host (the Mac MC runs on) — "Open on the Mac",
 * "Show in Finder". The server opens the file THERE, not on the device in
 * hand. So such a control shows only when the viewer sits at that Mac:
 *
 *  1. `useIsHostViewer()` — the page itself was opened via a loopback name
 *     (localhost, 127.0.0.1, ::1). A laptop or iPad that opens MC over
 *     Tailscale or the LAN uses another host name and never sees the
 *     controls (round-2 user tests t3 S1, r1 S4, t4, r2 N6: the click opened
 *     the file unseen on the Mac mini). Not a security boundary — the
 *     backend refuses the same requests with 403 (`app/services/host_open.py`,
 *     same rule, read from the Origin header).
 *  2. `HOST_ONLY_CLASS` — and only from `md` with a fine pointer (mouse or
 *     trackpad): never on a phone-sized screen (round-1 r2 S6: tapped by
 *     mistake next to the expand arrow).
 *
 * Known limit: Mark at the Mac itself, but with MC opened through its
 * Tailscale or LAN name, does not see them either — the browser cannot tell
 * that name apart from a second computer. Open http://localhost there.
 */
export const HOST_ONLY_CLASS = "hidden md:pointer-fine:inline-flex";

/** True for a host name that only ever means "this computer". Same rule as
 *  `is_loopback_host` in backend/app/services/host_open.py. */
export function isLoopbackHostname(hostname: string): boolean {
  const h = hostname.trim().toLowerCase().replace(/^\[(.*)\]$/, "$1").replace(/\.$/, "");
  if (h === "localhost" || h.endsWith(".localhost")) return true;
  if (h === "::1" || h === "0:0:0:0:0:0:0:1") return true;
  return /^127(?:\.\d{1,3}){3}$/.test(h);
}

const noSubscribe = () => () => undefined;
const readHostViewer = () => isLoopbackHostname(window.location.hostname);
// The server never renders host controls: the first client render agrees,
// then the real answer follows.
const serverHostViewer = () => false;

/** Does the viewer sit at the Mac MC runs on? (See the file comment.) */
export function useIsHostViewer(): boolean {
  return useSyncExternalStore(noSubscribe, readHostViewer, serverHostViewer);
}

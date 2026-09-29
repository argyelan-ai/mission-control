/**
 * Stream tickets — how the browser authenticates SSE streams and WebSockets.
 *
 * EventSource and WebSocket cannot send an Authorization header. The UI used
 * to append the login JWT as `?token=<jwt>`; every proxy/access/error log that
 * records the request URI then held a reusable 30-day operator credential in
 * plain text. Instead, right before EACH (re)connect we ask the backend for a
 * ticket (the JWT travels in the Authorization header of that POST) and put
 * only the ticket into the URL:
 *
 *   - opaque random value, no claims
 *   - bound to the user and to the exact stream path
 *   - valid ~60 s, consumed by the first connection
 *
 * A ticket is single-use, so a reconnect MUST fetch a new one — never reuse a
 * URL built earlier. Backend: backend/app/services/stream_tickets.py.
 */
import { BASE_URL, getToken } from "./authToken";

interface StreamTicketResponse {
  ticket: string;
  expires_in: number;
}

/** Mint a ticket for the stream at `path` (e.g. "/api/v1/agents/stream").
 *  The login token goes into the Authorization header of this POST only. */
export async function fetchStreamTicket(path: string): Promise<string> {
  const res = await fetch(`${BASE_URL}/api/v1/auth/stream-ticket`, {
    method: "POST",
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${getToken()}`,
    },
    body: JSON.stringify({ path }),
  });
  if (!res.ok) throw new Error(`stream ticket: HTTP ${res.status}`);
  const body = (await res.json()) as StreamTicketResponse;
  if (!body?.ticket) throw new Error("stream ticket: empty response");
  return body.ticket;
}

/**
 * Return `url` with a fresh `ticket` query parameter for its path. Works for
 * relative ("/api/v1/…"), http(s) and ws(s) URLs; keeps any existing query
 * parameters and never adds a `token` parameter.
 *
 * The ticket is bound to the path the BROWSER sees, and the backend compares
 * it with the path it receives. So NEXT_PUBLIC_API_URL must be origin-only
 * (no path prefix a proxy strips), and stream paths must not need
 * percent-encoding. Otherwise every stream fails closed with 401.
 */
export async function withStreamTicket(url: string): Promise<string> {
  const base =
    typeof window !== "undefined" ? window.location.href : "http://localhost/";
  const parsed = new URL(url, base);
  const ticket = await fetchStreamTicket(parsed.pathname);
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}ticket=${encodeURIComponent(ticket)}`;
}

const WS_BACKOFF_BASE_MS = 1_000;
const WS_BACKOFF_MAX_MS = 30_000;

/**
 * Open a same-origin WebSocket to `path` (e.g. "/api/v1/vault/stream") with
 * a fresh stream ticket. `setup` wires the handlers on every new socket.
 *
 * Reconnects by itself (1 s doubling up to 30 s, reset once a socket opens)
 * when the ticket request fails (backend restarting, Redis down) or the
 * socket closes unexpectedly (any code but 1000) — each attempt with a NEW
 * ticket, since tickets are single-use. Returns a cleanup that stops all
 * retries and closes the socket; use it as a useEffect cleanup.
 */
export function openTicketedWebSocket(
  path: string,
  setup: (ws: WebSocket) => void,
): () => void {
  let cancelled = false;
  let ws: WebSocket | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let attempt = 0;
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";

  const retry = () => {
    if (cancelled) return;
    const delay = Math.min(WS_BACKOFF_BASE_MS * 2 ** attempt, WS_BACKOFF_MAX_MS);
    attempt += 1;
    timer = setTimeout(connect, delay);
  };

  function connect() {
    timer = null;
    if (cancelled) return;
    withStreamTicket(`${proto}//${window.location.host}${path}`).then(
      (url) => {
        if (cancelled) return;
        const sock = new WebSocket(url);
        ws = sock;
        // addEventListener, not on*: `setup` owns the on* handlers.
        sock.addEventListener("open", () => {
          attempt = 0;
        });
        sock.addEventListener("close", (ev: CloseEvent) => {
          if (cancelled || ws !== sock) return;
          ws = null;
          if (ev.code !== 1000) retry();
        });
        setup(sock);
      },
      (err) => {
        if (cancelled) return;
        console.warn(`[stream ticket] ${path}:`, err);
        retry();
      },
    );
  }

  connect();
  return () => {
    cancelled = true;
    if (timer) clearTimeout(timer);
    ws?.close();
    ws = null;
  };
}

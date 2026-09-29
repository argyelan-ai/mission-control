/**
 * E0 page-usage beacon — reports the route *pattern* on every navigation
 * (backend: POST /api/v1/usage/page, rules in backend/app/services/usage_pages.py).
 * Only the pattern leaves the browser: no ids, no query string, no user.
 * Fire-and-forget: without a login token it does nothing, and a failure never
 * reaches the UI (no 401 redirect like request()).
 */
import { BASE_URL, getToken } from "./authToken";

const ID_LIKE =
  /^(?:\d+|[0-9a-f]{12,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/;
// Dynamic app routes (src/app/**/[param]) — their param segment is never sent.
const DYNAMIC_PARENTS = new Set(["agents", "schedule"]);

export function routePattern(pathname: string): string {
  const segments = pathname.split(/[?#]/)[0].toLowerCase().split("/").filter(Boolean);
  const out = segments.map((seg, i) =>
    (i === 1 && DYNAMIC_PARENTS.has(segments[0])) || ID_LIKE.test(seg) ? ":id" : seg,
  );
  return "/" + out.join("/");
}

export function sendPageView(pathname: string): void {
  const token = getToken();
  if (!token) return;
  fetch(`${BASE_URL}/api/v1/usage/page`, {
    method: "POST",
    keepalive: true,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ route: routePattern(pathname) }),
  }).catch(() => {});
}

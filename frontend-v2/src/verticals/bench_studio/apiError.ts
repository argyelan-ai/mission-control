/** request() throws `Error("API <status>: <raw body>")` — pull the backend's
 *  reason back out so a toast shows it instead of a generic failure.
 *
 *  Two body shapes reach the bench UI:
 *  - our own `HTTPException`: `{"detail": "Spark offline …"}` → the text;
 *  - FastAPI request validation (Pydantic, 422): `{"detail": [{loc, msg}, …]}`
 *    → the field name and the first message, e.g.
 *    `label: String should have at least 1 character` (+N when there are
 *    more). Round-2 user test t2 MUSS-1: this list used to fall through to
 *    "Challenge could not be started" with no reason at all.
 *
 *  Anything else (network error, HTML, empty detail) → ``fallback``. */
export function apiErrorDetail(err: unknown, fallback: string): string {
  if (!(err instanceof Error)) return fallback;
  const match = err.message.match(/^API \d+: ([\s\S]*)$/);
  if (!match) return fallback;
  let detail: unknown;
  try {
    detail = (JSON.parse(match[1]) as { detail?: unknown })?.detail;
  } catch {
    return fallback; // body wasn't JSON
  }
  if (typeof detail === "string") return detail.trim() || fallback;
  if (Array.isArray(detail)) return validationListText(detail) ?? fallback;
  return fallback;
}

type ValidationItem = { loc?: unknown; msg?: unknown };

function validationListText(items: ValidationItem[]): string | null {
  const first = items[0];
  if (!first || typeof first.msg !== "string" || !first.msg.trim()) return null;
  // loc = ["body", "models", 0, "label"] → the last named part is the field.
  // "body" alone names the whole request, not a field.
  const names = Array.isArray(first.loc)
    ? first.loc.filter((part): part is string => typeof part === "string" && part !== "body")
    : [];
  const field = names.at(-1);
  const text = field ? `${field}: ${first.msg.trim()}` : first.msg.trim();
  return items.length > 1 ? `${text} (+${items.length - 1})` : text;
}

/** One Pydantic validation problem, with the request path inside the body
 *  (`["title"]`, `["models", 1, "label"]`). */
export type ApiFieldError = {
  path: (string | number)[];
  type: string;
  msg: string;
  ctx?: Record<string, unknown>;
};

/** The validation list of a FastAPI 422, so a form can say each problem at
 *  its own field (round-4 K1, t2 SOLL-2: the 422 only reached a raw English
 *  toast). Empty for any other error — the caller then falls back to
 *  :func:`apiErrorDetail`. */
export function apiFieldErrors(err: unknown): ApiFieldError[] {
  if (!(err instanceof Error)) return [];
  const match = err.message.match(/^API 422: ([\s\S]*)$/);
  if (!match) return [];
  let detail: unknown;
  try {
    detail = (JSON.parse(match[1]) as { detail?: unknown })?.detail;
  } catch {
    return [];
  }
  if (!Array.isArray(detail)) return [];
  const out: ApiFieldError[] = [];
  for (const item of detail as (ValidationItem & { type?: unknown; ctx?: unknown })[]) {
    if (!Array.isArray(item?.loc)) continue;
    const path = item.loc
      .filter((part): part is string | number => typeof part === "string" || typeof part === "number")
      .filter((part, i) => !(i === 0 && part === "body"));
    const entry: ApiFieldError = {
      path,
      type: typeof item.type === "string" ? item.type : "",
      msg: typeof item.msg === "string" ? item.msg : "",
    };
    if (item.ctx && typeof item.ctx === "object") entry.ctx = item.ctx as Record<string, unknown>;
    out.push(entry);
  }
  return out;
}

/** The toast text for validation problems a form has no field for: the
 *  first one as `field: message`, `(+N)` when there are more — the same
 *  shape :func:`apiErrorDetail` gives a whole list. Null when the first has
 *  no message. */
export function apiFieldErrorsText(items: ApiFieldError[]): string | null {
  const first = items[0];
  const msg = first?.msg.trim();
  if (!msg) return null;
  const field = first.path.filter((part): part is string => typeof part === "string").at(-1);
  const text = field ? `${field}: ${msg}` : msg;
  return items.length > 1 ? `${text} (+${items.length - 1})` : text;
}

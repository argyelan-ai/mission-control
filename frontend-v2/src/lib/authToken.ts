/**
 * The login token and the API base URL — dependency-free on purpose, so that
 * low-level helpers (lib/streamTicket.ts) can use them without importing the
 * whole API client (no import cycle, and a test that mocks "@/lib/api" does
 * not silently break stream authentication). Re-exported by lib/api.ts.
 */
export const BASE_URL = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/$/, "");

export const AUTH_TOKEN_KEY = "mc_auth_token";

export function getToken(): string {
  if (typeof window === "undefined") return "";
  return localStorage.getItem(AUTH_TOKEN_KEY) ?? "";
}

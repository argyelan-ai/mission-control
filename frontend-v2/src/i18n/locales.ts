// Supported UI locales — dependency-free (no next/headers), so both the
// server config (i18n/request.ts) and client code (lib/locale.ts) can use it.
export const LOCALES = ["en", "de"] as const;
export type Locale = (typeof LOCALES)[number];
export const DEFAULT_LOCALE: Locale = "en";
export const LOCALE_COOKIE = "NEXT_LOCALE";

export function isLocale(value: unknown): value is Locale {
  return typeof value === "string" && (LOCALES as readonly string[]).includes(value);
}

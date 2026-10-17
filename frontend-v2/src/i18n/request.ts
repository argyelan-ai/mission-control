import { getRequestConfig } from "next-intl/server";
import { cookies, headers } from "next/headers";

// UI locales. Deliberately cookie-based, NOT URL-based (/de/...): MC is an
// app behind a login, URLs stay stable, no SEO concern. The cookie is set by
// the language selector in Settings → Profile; `router.refresh()` re-renders
// the server tree with the new locale immediately.
//
// Scope note: this is UI i18n only. Agent response language is a separate,
// per-agent concern (`agents.language` → SOUL template), and the agent
// templates themselves stay English (model instructions).
export const LOCALES = ["en", "de"] as const;
export type Locale = (typeof LOCALES)[number];
export const DEFAULT_LOCALE: Locale = "en";
export const LOCALE_COOKIE = "NEXT_LOCALE";

function isLocale(value: string | undefined | null): value is Locale {
  return !!value && (LOCALES as readonly string[]).includes(value);
}

// First visit, no saved choice yet: read the browser's language from the
// Accept-Language header (the server-side equivalent of navigator.language —
// it reflects the same browser setting, before any client JS runs) and
// preselect a supported locale from it. A saved cookie always wins; this
// only fills the gap before the operator has ever picked a language in
// Settings → Profile.
export function localeFromAcceptLanguage(header: string | null | undefined): Locale | null {
  if (!header) return null;
  for (const part of header.split(",")) {
    const tag = part.trim().split(";")[0]?.toLowerCase();
    const primary = tag?.split("-")[0];
    if (isLocale(primary)) return primary;
  }
  return null;
}

export function pickLocale(
  cookieValue: string | undefined | null,
  acceptLanguageHeader: string | null | undefined,
): Locale {
  if (isLocale(cookieValue)) return cookieValue;
  return localeFromAcceptLanguage(acceptLanguageHeader) ?? DEFAULT_LOCALE;
}

export default getRequestConfig(async () => {
  const [store, headerStore] = await Promise.all([cookies(), headers()]);
  const locale = pickLocale(
    store.get(LOCALE_COOKIE)?.value,
    headerStore.get("accept-language"),
  );

  return {
    locale,
    messages: (await import(`../../messages/${locale}.json`)).default,
  };
});

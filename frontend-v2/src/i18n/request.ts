import { getRequestConfig } from "next-intl/server";
import { cookies } from "next/headers";

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

export default getRequestConfig(async () => {
  const store = await cookies();
  const raw = store.get(LOCALE_COOKIE)?.value;
  const locale: Locale = LOCALES.includes(raw as Locale)
    ? (raw as Locale)
    : DEFAULT_LOCALE;

  return {
    locale,
    messages: (await import(`../../messages/${locale}.json`)).default,
  };
});

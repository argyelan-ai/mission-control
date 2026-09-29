/**
 * Home alert strip — alerts contributed by verticals via
 * GET /api/v1/system/alerts (backend hooks.home_alert_providers).
 *
 * The backend computes them live from state, so an alert disappears by
 * itself once its cause is fixed. Here we only localize and colour them for
 * the existing warning strip on the Home page.
 */

export type Localized = string | Record<string, string> | null | undefined;

export interface HomeAlert {
  id: string;
  severity: "info" | "warning" | "critical";
  title: Localized;
  detail: Localized;
  href: string | null;
}

export interface HomeAlertsResponse {
  alerts: HomeAlert[];
}

export interface BannerAlert {
  key: string;
  label: string;
  color: string;
  href: string | undefined;
  title: string | undefined;
}

/** Pick the operator's language; fall back to English, then to any value. */
export function pickLocalized(value: Localized, locale: string): string {
  if (!value) return "";
  if (typeof value === "string") return value;
  return value[locale] ?? value.en ?? Object.values(value).find((v) => typeof v === "string" && v) ?? "";
}

export function homeAlertsToBanner(
  alerts: HomeAlert[] | undefined,
  locale: string,
  colors: { warning: string; error: string },
): BannerAlert[] {
  return (alerts ?? []).map((a) => ({
    key: a.id,
    label: pickLocalized(a.title, locale),
    color: a.severity === "critical" ? colors.error : colors.warning,
    href: a.href ?? undefined,
    title: pickLocalized(a.detail, locale) || undefined,
  }));
}

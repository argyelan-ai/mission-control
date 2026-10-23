"use client";

/**
 * Any URL that matches no page (round-3 t4 S9). Next's own 404 is English,
 * white and has no menu — on the phone only the browser's back button led
 * out. This one keeps the app frame (sidebar / tab bar), speaks the UI
 * language and offers the way home.
 */
import { useTranslations } from "next-intl";
import AppShell from "@/components/layout/AppShell";
import { NotFoundState } from "@/components/shared/NotFoundState";

export default function NotFound() {
  const t = useTranslations("notFound");
  return (
    <AppShell>
      <NotFoundState title={t("title")} hint={t("hint")} backHref="/" backLabel={t("home")} />
    </AppShell>
  );
}

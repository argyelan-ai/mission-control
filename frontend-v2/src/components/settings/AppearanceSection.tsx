"use client";

/**
 * Settings → Appearance (ADR-087). Dark is Mission Control's default and
 * character; light and "follow the system" are options, saved per browser.
 */
import { useTranslations } from "next-intl";
import { ThemeSegmented } from "@/components/shared/ThemeSwitch";

export function AppearanceSection() {
  const t = useTranslations("theme");
  return (
    <div>
      <div className="mb-6">
        <h2 className="text-base font-semibold" style={{ color: "var(--color-text-primary)" }}>
          {t("sectionTitle")}
        </h2>
        <p className="text-sm mt-1" style={{ color: "var(--color-text-muted)" }}>
          {t("sectionDescription")}
        </p>
      </div>
      <div
        className="p-6"
        style={{
          background: "var(--color-bg-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: 12,
        }}
      >
        <div className="text-sm font-medium mb-3" style={{ color: "var(--color-text-primary)" }}>
          {t("label")}
        </div>
        <ThemeSegmented />
        <p className="text-xs mt-3" style={{ color: "var(--color-text-muted)" }}>
          {t("hint")}
        </p>
      </div>
    </div>
  );
}

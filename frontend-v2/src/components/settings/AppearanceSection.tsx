"use client";

/**
 * AppearanceSection — theme choice (dark · light · system), ADR-084.
 * Stored per browser (lib/theme.ts); applied immediately, no reload.
 */

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Moon, Sun, Monitor } from "lucide-react";
import { C } from "@/lib/colors";
import { readStoredTheme, setTheme, type ThemeChoice } from "@/lib/theme";

const OPTIONS: { value: ThemeChoice; icon: typeof Moon; labelKey: string; descKey: string }[] = [
  { value: "dark", icon: Moon, labelKey: "dark", descKey: "darkDesc" },
  { value: "light", icon: Sun, labelKey: "light", descKey: "lightDesc" },
  { value: "system", icon: Monitor, labelKey: "system", descKey: "systemDesc" },
];

export function AppearanceSection() {
  const t = useTranslations("settings.appearance");
  const [choice, setChoice] = useState<ThemeChoice>("dark");

  // Read after mount — localStorage is client-only.
  useEffect(() => {
    setChoice(readStoredTheme());
  }, []);

  const pick = (v: ThemeChoice) => {
    setChoice(v);
    setTheme(v);
  };

  return (
    <div>
      <div className="mb-6">
        <h2 className="text-base font-semibold" style={{ color: C.textPrimary }}>
          {t("title")}
        </h2>
        <p className="text-sm mt-1" style={{ color: C.textMuted }}>
          {t("description")}
        </p>
      </div>

      <div role="radiogroup" aria-label={t("title")} className="grid gap-3 sm:grid-cols-3">
        {OPTIONS.map(({ value, icon: Icon, labelKey, descKey }) => {
          const on = choice === value;
          return (
            <button
              key={value}
              type="button"
              role="radio"
              aria-checked={on}
              onClick={() => pick(value)}
              className="text-left rounded-xl px-4 py-4 transition-colors cursor-pointer"
              style={{
                background: on ? C.bgSurface : "transparent",
                border: `1px solid ${on ? C.borderAccent : C.border}`,
                color: C.textPrimary,
              }}
            >
              <div className="flex items-center gap-2 mb-1.5">
                <Icon size={15} style={{ color: on ? C.accent : C.textMuted }} />
                <span className="text-sm font-medium">{t(labelKey)}</span>
              </div>
              <div className="text-xs" style={{ color: C.textMuted }}>
                {t(descKey)}
              </div>
            </button>
          );
        })}
      </div>

      <p className="text-xs mt-4" style={{ color: C.textDim }}>
        {t("note")}
      </p>
    </div>
  );
}

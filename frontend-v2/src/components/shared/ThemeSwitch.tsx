"use client";

/**
 * Theme switch (ADR-087) — Dark (default) · Light · System, saved per browser.
 *
 *  - ThemeSegmented: the full three-way radio group (Settings → Appearance,
 *    mobile menu). Arrow keys move the choice, like any radio group.
 *    size="touch" (phone menu) makes every option a 44px target.
 *  - ThemeCycleButton: one compact icon button for the desktop user menu
 *    (sidebar footer), cycling Dark → Light → System. Its label always says
 *    what is active and what a click does.
 *
 * Both read the same store (useTheme), so every switch on the page stays in
 * step, and a choice made in another tab arrives here too.
 */
import { useRef, type CSSProperties, type KeyboardEvent } from "react";
import { useTranslations } from "next-intl";
import { Monitor, Moon, Sun, type LucideIcon } from "lucide-react";
import { THEME_CHOICES, useTheme, type ThemeChoice } from "@/lib/theme";

const ICON: Record<ThemeChoice, LucideIcon> = { dark: Moon, light: Sun, system: Monitor };

export function ThemeSegmented({ size = "md", className }: { size?: "sm" | "md" | "touch"; className?: string }) {
  const t = useTranslations("theme");
  const { choice, setTheme } = useTheme();
  const refs = useRef<Record<ThemeChoice, HTMLButtonElement | null>>({ dark: null, light: null, system: null });
  // Option height: the group adds 4px padding on each side.
  const optionHeight = size === "touch" ? 44 : size === "sm" ? 22 : 26;
  const small = size === "sm";

  function onKey(e: KeyboardEvent<HTMLButtonElement>, current: ThemeChoice) {
    const step = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
    if (!step) return;
    e.preventDefault();
    const i = THEME_CHOICES.indexOf(current);
    const next = THEME_CHOICES[(i + step + THEME_CHOICES.length) % THEME_CHOICES.length];
    setTheme(next);
    refs.current[next]?.focus();
  }

  return (
    <div
      role="radiogroup"
      aria-label={t("label")}
      className={`inline-flex items-center gap-1 p-1 ${className ?? ""}`}
      style={{
        background: "var(--color-bg-base)",
        border: "1px solid var(--color-border)",
        borderRadius: "var(--radius-full)",
      }}
    >
      {THEME_CHOICES.map((c) => {
        const Icon = ICON[c];
        const active = c === choice;
        const style: CSSProperties = {
          height: optionHeight,
          padding: small ? "0 10px" : "0 14px",
          borderRadius: "var(--radius-full)",
          fontSize: small ? 12 : 13,
          fontWeight: active ? 600 : 500,
          background: active ? "var(--color-bg-elevated)" : "transparent",
          color: active ? "var(--color-text-primary)" : "var(--color-text-muted)",
          boxShadow: active ? "inset 0 0 0 1px var(--color-border-strong)" : "none",
        };
        return (
          <button
            key={c}
            ref={(el) => { refs.current[c] = el; }}
            type="button"
            role="radio"
            aria-checked={active}
            tabIndex={active ? 0 : -1}
            onClick={() => setTheme(c)}
            onKeyDown={(e) => onKey(e, c)}
            className="inline-flex items-center gap-1.5 cursor-pointer transition-colors"
            style={style}
          >
            <Icon size={small ? 13 : 14} strokeWidth={1.75} aria-hidden />
            {t(c)}
          </button>
        );
      })}
    </div>
  );
}

export function ThemeCycleButton({ size = 26, style }: { size?: number; style?: CSSProperties }) {
  const t = useTranslations("theme");
  const { choice, setTheme } = useTheme();
  const next = THEME_CHOICES[(THEME_CHOICES.indexOf(choice) + 1) % THEME_CHOICES.length];
  const Icon = ICON[choice];
  const label = t("cycleLabel", { current: t(choice), next: t(next) });
  return (
    <button
      type="button"
      data-testid="theme-cycle"
      data-choice={choice}
      onClick={() => setTheme(next)}
      aria-label={label}
      title={label}
      className="grid place-items-center shrink-0 cursor-pointer"
      style={{
        width: size,
        height: size,
        borderRadius: "var(--radius-full)",
        color: "var(--color-p2-faint)",
        ...style,
      }}
      onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.color = "var(--color-p2-txt)")}
      onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.color = (style?.color as string) ?? "var(--color-p2-faint)")}
    >
      <Icon size={14} strokeWidth={1.75} aria-hidden />
    </button>
  );
}

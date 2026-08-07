"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { ChevronRight } from "lucide-react";
import { C } from "@/lib/colors";
import { cn } from "@/lib/utils";

/**
 * One section header for the whole app.
 *
 * /runtimes grew six sections written by different hands, each with its own
 * header markup and its own badge styling (grey rounded chip, mono `label-sys`
 * chip, small-caps chip). This is the single vocabulary: mono instrument label
 * for the count, one title size, one hint tone, one actions slot on the right.
 *
 * Sections default to open, so collapsing is purely additive for the operator.
 * The open state is remembered per id in localStorage.
 */

const storageKey = (id: string) => `mc-section-${id}`;
const OPEN_EVENT = "mc:section-open";

function readStored(id: string): boolean | null {
  try {
    const v = localStorage.getItem(storageKey(id));
    return v === null ? null : v === "1";
  } catch {
    return null;
  }
}

/** Open a section from outside (used by SectionNav before scrolling to it). */
export function requestSectionOpen(id: string) {
  try {
    localStorage.setItem(storageKey(id), "1");
  } catch {
    /* private mode — the event below still opens it for this render */
  }
  window.dispatchEvent(new CustomEvent(OPEN_EVENT, { detail: id }));
}

export interface SectionProps {
  /** Anchor id — also the localStorage key for the collapsed state. */
  id: string;
  title: string;
  /** One line under the title. Keep it to what the section is for. */
  hint?: string;
  /** Rendered as a mono count chip next to the title. */
  count?: number;
  /** Extra chip after the count (e.g. "2 new"). */
  badge?: React.ReactNode;
  /** Buttons on the right of the header. Never collapse-toggles. */
  actions?: React.ReactNode;
  collapsible?: boolean;
  children: React.ReactNode;
  className?: string;
}

export function Section({
  id,
  title,
  hint,
  count,
  badge,
  actions,
  collapsible = true,
  children,
  className,
}: SectionProps) {
  const t = useTranslations("common.section");
  const [open, setOpen] = useState(true);

  useEffect(() => {
    const stored = readStored(id);
    if (stored !== null) setOpen(stored);
  }, [id]);

  useEffect(() => {
    const onOpen = (e: Event) => {
      if ((e as CustomEvent<string>).detail === id) setOpen(true);
    };
    window.addEventListener(OPEN_EVENT, onOpen);
    return () => window.removeEventListener(OPEN_EVENT, onOpen);
  }, [id]);

  const toggle = useCallback(() => {
    setOpen((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(storageKey(id), next ? "1" : "0");
      } catch {
        /* ignore */
      }
      return next;
    });
  }, [id]);

  const heading = (
    <div className="min-w-0 flex-1 text-left">
      <div className="flex items-center gap-2 flex-wrap">
        <h2 className="text-sm font-semibold" style={{ color: C.textPrimary }}>
          {title}
        </h2>
        {count != null && (
          <span
            className="label-sys tabular-nums rounded-sm px-1.5 py-0.5"
            style={{ background: C.border }}
            data-testid={`section-count-${id}`}
          >
            {count}
          </span>
        )}
        {badge}
      </div>
      {hint && (
        <p className="text-xs mt-0.5" style={{ color: C.textMuted }}>
          {hint}
        </p>
      )}
    </div>
  );

  return (
    <section id={id} className={cn("mt-8 scroll-mt-4 first:mt-0", className)} data-testid={`section-${id}`}>
      <div className="flex items-center gap-2 mb-3">
        {collapsible ? (
          <button
            type="button"
            onClick={toggle}
            aria-expanded={open}
            aria-controls={`${id}-body`}
            aria-label={open ? t("collapse", { name: title }) : t("expand", { name: title })}
            data-testid={`section-toggle-${id}`}
            className="flex items-center gap-2 min-w-0 flex-1 rounded-md py-1 pr-2 cursor-pointer transition-colors hover:bg-[var(--color-bg-surface)]"
          >
            <ChevronRight
              size={14}
              className="shrink-0 transition-transform duration-200 motion-reduce:transition-none"
              style={{ color: C.textMuted, transform: open ? "rotate(90deg)" : "none" }}
            />
            {heading}
          </button>
        ) : (
          <div className="flex items-center gap-2 min-w-0 flex-1 py-1 pr-2">{heading}</div>
        )}
        {actions && <div className="flex items-center gap-1.5 shrink-0">{actions}</div>}
      </div>

      {open && (
        <div id={`${id}-body`} className="min-w-0">
          {children}
        </div>
      )}
    </section>
  );
}

/**
 * A Section that can opt out of its own chrome.
 *
 * The model surfaces (provider catalog, local recipes, download) each used to
 * be a top-level section. They now live as tabs inside one "Models" section,
 * so they render their body only — but they still work standalone, which is
 * what keeps their tests and any other mounting point valid.
 */
export function SectionOrFragment({
  embedded,
  embeddedTitle = true,
  children,
  ...section
}: SectionProps & {
  embedded?: boolean;
  /** false when the parent already names this surface (e.g. a tab label). */
  embeddedTitle?: boolean;
}) {
  if (!embedded) return <Section {...section}>{children}</Section>;

  // Embedded is a demotion, not a deletion: the sub-heading is one type step
  // quieter than a real section header, but the count and the section's own
  // actions (add a box, check for updates) have to survive the move.
  return (
    <div id={section.id} data-testid={`section-${section.id}`} className="scroll-mt-4">
      <div className="flex items-center gap-2 mb-2">
        <div className="min-w-0 flex-1 flex items-center gap-2 flex-wrap">
          {embeddedTitle && (
            <>
              <h3 className="label-sys" style={{ color: C.textSecondary }}>
                {section.title}
              </h3>
              {section.count != null && (
                <span className="label-sys tabular-nums" style={{ color: C.textDim }}>
                  {section.count}
                </span>
              )}
            </>
          )}
          {embeddedTitle && section.badge}
        </div>
        {(section.actions || (!embeddedTitle && section.badge)) && (
          // Without a title the badge would sit alone on an otherwise empty
          // line; keep it with the actions so the row reads as one strip.
          <div className="flex items-center gap-1.5 shrink-0">
            {!embeddedTitle && section.badge}
            {section.actions}
          </div>
        )}
      </div>
      {children}
    </div>
  );
}

export interface SectionNavItem {
  id: string;
  label: string;
  count?: number;
}

/**
 * Jump bar for long pages. /runtimes is 2337 px tall at 1440 px wide, so the
 * operator's only way to reach CLI-Tools was to scroll past everything.
 */
export function SectionNav({ items }: { items: SectionNavItem[] }) {
  const t = useTranslations("common.section");

  const jump = (id: string) => {
    requestSectionOpen(id);
    // The section may have been collapsed; let it render before scrolling.
    requestAnimationFrame(() => {
      document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  };

  return (
    <nav
      aria-label={t("jumpTo")}
      // Sticky on mobile: the page is ~2900px there, so the three stations have
      // to stay reachable without scrolling back to the top. Solid surface, not
      // a blur — the palette bans glass as decoration.
      className="sticky top-0 z-20 -mx-4 px-4 sm:static sm:mx-0 sm:px-0 flex items-center gap-1 overflow-x-auto sm:flex-wrap mb-4 sm:mb-6 py-2 sm:py-0 sm:pb-3"
      style={{
        borderBottom: `1px solid ${C.borderSubtle}`,
        backgroundColor: "var(--color-p2-bg)",
      }}
      data-testid="section-nav"
    >
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          onClick={() => jump(item.id)}
          className="inline-flex items-center gap-1.5 rounded-md px-3 py-2 min-h-11 sm:min-h-0 sm:px-2 sm:py-1.5 text-xs cursor-pointer transition-colors hover:bg-[var(--color-bg-surface)]"
          style={{ color: C.textSecondary }}
        >
          {item.label}
          {item.count != null && (
            <span className="label-sys tabular-nums" style={{ color: C.textDim }}>
              {item.count}
            </span>
          )}
        </button>
      ))}
    </nav>
  );
}

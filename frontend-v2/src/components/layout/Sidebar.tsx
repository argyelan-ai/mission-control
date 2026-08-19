"use client";

import { useEffect, useMemo, useState } from "react";
import { usePathname } from "next/navigation";
import Link from "next/link";
import { motion, AnimatePresence } from "framer-motion";
import { Search, Pin, PinOff, ArrowUp, ArrowDown, ChevronDown, MoreHorizontal } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useAppStore } from "@/lib/store";
import { api } from "@/lib/api";
import type { SystemMetrics } from "@/lib/types";
import { VoiceButton } from "@/components/voice/VoiceWidget";
import BoardPicker from "./BoardPicker";
import SidebarFooter from "./SidebarFooter";
import {
  DEFAULT_PINS,
  NAV_ITEMS,
  NAV_TREE,
  resolveNav,
  isActiveRoute,
  groupKeyFor,
  type NavItem,
} from "@/lib/nav";

// Re-exported for existing consumers (MobileNav). The model lives in lib/nav.
export { NAV_ITEMS, NAV_TREE };

const MONO = { fontFamily: "var(--font-p2-mono)" };
const EASE = [0.16, 1, 0.3, 1] as const;
const EMPTY_GROUP_STATE: Record<string, boolean> = {};
const noop = () => {};

type Ctx = { href: string; x: number; y: number; pinned: boolean } | null;

export default function Sidebar() {
  const t = useTranslations("nav");
  const tShell = useTranslations("shell");
  const pathname = usePathname();
  const store = useAppStore();
  const { sidebarCollapsed, setCommandPaletteOpen } = store;
  // Fall back rather than crash: state persisted by an older build carries no
  // pinnedNav, and partial store mocks in tests carry neither.
  const pinnedNav = store.pinnedNav ?? DEFAULT_PINS;
  const navGroupState = store.navGroupState ?? EMPTY_GROUP_STATE;
  const setPinnedNav = store.setPinnedNav ?? noop;
  const togglePin = store.togglePin ?? noop;
  const setNavGroupOpen = store.setNavGroupOpen ?? noop;

  const [ctx, setCtx] = useState<Ctx>(null);

  const { data: metrics } = useQuery<SystemMetrics>({
    queryKey: ["system-metrics"],
    queryFn: api.system.metrics,
    refetchInterval: 30_000,
  });

  const { pinned, groups } = useMemo(() => resolveNav(pinnedNav), [pinnedNav]);

  // The group holding the current route counts as open unless the user
  // explicitly closed it. Derived, never written during render — a store write
  // in render would persist to localStorage on every mount.
  const activeGroupKey = useMemo(() => {
    const current = NAV_ITEMS.find((i) => isActiveRoute(i.href, pathname));
    if (!current || pinnedNav.includes(current.href)) return undefined;
    return groupKeyFor(current.href);
  }, [pathname, pinnedNav]);

  const isGroupOpen = (key: string) => navGroupState[key] ?? key === activeGroupKey;

  useEffect(() => {
    if (!ctx) return;
    const close = () => setCtx(null);
    document.addEventListener("click", close);
    document.addEventListener("scroll", close, true);
    return () => {
      document.removeEventListener("click", close);
      document.removeEventListener("scroll", close, true);
    };
  }, [ctx]);

  /** Badge for a route — only real state, never decoration. */
  function badgeFor(href: string): { text?: string; alert?: boolean } {
    if (!metrics) return {};
    if (href === "/tasks" && metrics.tasks.active > 0) return { text: String(metrics.tasks.active) };
    if (href === "/agents") return { text: `${metrics.agents.online}/${metrics.agents.total}` };
    if (href === "/inbox" && metrics.approvals.pending > 0) return { alert: true };
    return {};
  }

  function movePin(href: string, delta: number) {
    const i = pinnedNav.indexOf(href);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= pinnedNav.length) return;
    const next = [...pinnedNav];
    [next[i], next[j]] = [next[j], next[i]];
    setPinnedNav(next);
  }

  const width = sidebarCollapsed ? 64 : 248;

  // ── one nav row, used for pins and for group children ─────────────────────
  function Row({ item, nested = false }: { item: NavItem; nested?: boolean }) {
    const active = isActiveRoute(item.href, pathname);
    const { text, alert } = badgeFor(item.href);
    const Icon = item.icon;
    const isPinned = pinnedNav.includes(item.href);
    const label = t(item.labelKey) || item.label;

    if (sidebarCollapsed) {
      return (
        <Link
          href={item.href}
          title={label}
          aria-current={active ? "page" : undefined}
          className="group relative grid place-items-center shrink-0"
          style={{
            width: 44,
            height: 38,
            borderRadius: "12px",
            backgroundColor: active ? "var(--color-accent-subtle)" : "transparent",
            color: active ? "var(--color-p2-txt)" : "var(--color-p2-dim)",
          }}
        >
          <Icon size={16} strokeWidth={active ? 2 : 1.75} />
          {alert && (
            <span
              className="absolute"
              style={{
                top: 6,
                right: 9,
                width: 6,
                height: 6,
                borderRadius: "999px",
                backgroundColor: "var(--color-p2-err)",
              }}
            />
          )}
          <span
            className="absolute left-full ml-2 px-2 py-1 whitespace-nowrap opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity z-50"
            style={{
              ...MONO,
              fontSize: "11px",
              backgroundColor: "var(--color-p2-pan2)",
              border: "1px solid var(--color-p2-line)",
              borderRadius: "8px",
              color: "var(--color-p2-txt)",
              boxShadow: "var(--shadow-elevated)",
            }}
          >
            {label}
          </span>
        </Link>
      );
    }

    return (
      <Link
        href={item.href}
        aria-current={active ? "page" : undefined}
        onContextMenu={(e) => {
          e.preventDefault();
          setCtx({ href: item.href, x: e.clientX, y: e.clientY, pinned: isPinned });
        }}
        className="group flex items-center gap-2.5"
        style={{
          height: nested ? 32 : 38,
          padding: nested ? "0 12px 0 30px" : "0 12px",
          borderRadius: "12px",
          ...MONO,
          fontSize: nested ? "11.5px" : "12.5px",
          fontWeight: active ? 700 : 400,
          color: active ? "var(--color-p2-txt)" : "var(--color-p2-dim)",
          backgroundColor: active ? "var(--color-accent-subtle)" : "transparent",
          transition: "background-color 160ms ease, color 160ms ease",
        }}
        onMouseEnter={(e) => {
          if (!active) {
            const el = e.currentTarget as HTMLElement;
            el.style.backgroundColor = "var(--color-p2-pan2)";
            el.style.color = "var(--color-p2-txt)";
          }
        }}
        onMouseLeave={(e) => {
          if (!active) {
            const el = e.currentTarget as HTMLElement;
            el.style.backgroundColor = "transparent";
            el.style.color = "var(--color-p2-dim)";
          }
        }}
      >
        <Icon size={nested ? 14 : 16} strokeWidth={active ? 2 : 1.75} className="shrink-0" />
        <span className="truncate">{label}</span>

        {alert ? (
          <span
            className="ml-auto shrink-0"
            style={{ width: 6, height: 6, borderRadius: "999px", backgroundColor: "var(--color-p2-err)" }}
          />
        ) : text ? (
          <span
            className="ml-auto shrink-0 tabular-nums"
            style={{ fontSize: "10px", color: active ? "var(--color-p2-dim)" : "var(--color-p2-faint)" }}
          >
            {text}
          </span>
        ) : null}

        {/* Pin toggle — appears on hover, never competes with the badge */}
        <button
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            togglePin(item.href);
          }}
          aria-label={`${label} — ${isPinned ? t("unpin") : t("pin")}`}
          title={isPinned ? t("unpin") : t("pin")}
          className="shrink-0 opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity cursor-pointer grid place-items-center"
          style={{
            width: 18,
            height: 18,
            marginLeft: alert || text ? 6 : "auto",
            borderRadius: "6px",
            color: "var(--color-p2-faint)",
          }}
        >
          {isPinned ? <PinOff size={11} /> : <Pin size={11} />}
        </button>
      </Link>
    );
  }

  return (
    <motion.aside
      animate={{ width }}
      transition={{ duration: 0.2, ease: EASE }}
      className="flex flex-col h-full overflow-hidden shrink-0"
      style={{
        backgroundColor: "var(--color-p2-pan)",
        borderRight: "1px solid var(--color-p2-line2)",
        padding: sidebarCollapsed ? "10px 0" : "10px",
      }}
    >
      {/* Zone 1 — where am I */}
      <div className={sidebarCollapsed ? "px-2.5" : ""}>
        <BoardPicker collapsed={sidebarCollapsed} />
      </div>

      {/* Zone 2 — search / jump */}
      {sidebarCollapsed ? (
        <div className="flex flex-col items-center gap-1 mt-2">
          <button
            onClick={() => setCommandPaletteOpen(true)}
            aria-label={`${tShell("search")} (⌘K)`}
            title={`${tShell("search")} · ⌘K`}
            className="grid place-items-center cursor-pointer"
            style={{ width: 44, height: 38, borderRadius: "12px", color: "var(--color-p2-dim)" }}
          >
            <Search size={16} />
          </button>
          <VoiceButton size={30} variant="sidebar" enforceTouchTarget={false} />
        </div>
      ) : (
        <div
          className="flex items-center gap-2 mt-2 shrink-0"
          style={{
            height: 36,
            padding: "0 4px 0 12px",
            borderRadius: "12px",
            backgroundColor: "var(--color-p2-inset)",
            border: "1px solid var(--color-p2-line2)",
          }}
        >
          <button
            onClick={() => setCommandPaletteOpen(true)}
            className="flex items-center gap-2 flex-1 min-w-0 cursor-pointer text-left"
            style={{ ...MONO, fontSize: "11.5px", color: "var(--color-p2-faint)" }}
          >
            <Search size={13} className="shrink-0" />
            <span className="truncate">{tShell("search")}</span>
            <kbd
              className="ml-auto shrink-0 px-1.5"
              style={{
                border: "1px solid var(--color-p2-line)",
                borderRadius: "6px",
                fontSize: "9.5px",
                color: "var(--color-p2-dim)",
              }}
            >
              ⌘K
            </kbd>
          </button>
          <VoiceButton size={28} variant="sidebar" enforceTouchTarget={false} />
        </div>
      )}

      {/* Zones 3 + 4 — pinned rows, then the rest of the tree one click deep */}
      <nav
        className={`flex-1 overflow-y-auto overflow-x-hidden mt-4 flex flex-col ${
          sidebarCollapsed ? "items-center gap-1" : "gap-0.5"
        }`}
      >
        {pinned.map((item) => (
          <Row key={item.href} item={item} />
        ))}

        {pinned.length > 0 && groups.length > 0 && (
          <div
            style={{
              height: 1,
              backgroundColor: "var(--color-p2-line2)",
              margin: sidebarCollapsed ? "8px 0 4px" : "10px 12px 6px",
              width: sidebarCollapsed ? 34 : "auto",
            }}
          />
        )}

        {sidebarCollapsed && groups.length > 0 && (
          <button
            onClick={() => setCommandPaletteOpen(true)}
            title={`${t("moreAreas")} (${groups.reduce((n, g) => n + g.children.length, 0)}) · ⌘K`}
            aria-label={t("moreAreas")}
            className="grid place-items-center cursor-pointer shrink-0"
            style={{ width: 44, height: 38, borderRadius: "12px", color: "var(--color-p2-faint)" }}
          >
            <MoreHorizontal size={16} />
          </button>
        )}

        {sidebarCollapsed
          ? // Collapsed shows the pins and nothing else — a 64px column of all
            // 19 icons would be the old wall of choices in miniature. Everything
            // unpinned is one ⌘K away.
            null
          : groups.map((group) => {
              const open = isGroupOpen(group.key);
              const GroupIcon = group.icon;
              const holdsActive = group.children.some((c) => isActiveRoute(c.href, pathname));
              return (
                <div key={group.key}>
                  <button
                    onClick={() => setNavGroupOpen(group.key, !open)}
                    aria-expanded={open}
                    className="w-full flex items-center gap-2.5 cursor-pointer"
                    style={{
                      height: 34,
                      padding: "0 12px",
                      borderRadius: "12px",
                      ...MONO,
                      fontSize: "12px",
                      color: holdsActive && !open ? "var(--color-p2-txt)" : "var(--color-p2-dim)",
                      transition: "color 160ms ease, background-color 160ms ease",
                    }}
                    onMouseEnter={(e) => {
                      (e.currentTarget as HTMLElement).style.backgroundColor = "var(--color-p2-pan2)";
                      (e.currentTarget as HTMLElement).style.color = "var(--color-p2-txt)";
                    }}
                    onMouseLeave={(e) => {
                      (e.currentTarget as HTMLElement).style.backgroundColor = "transparent";
                      (e.currentTarget as HTMLElement).style.color =
                        holdsActive && !open ? "var(--color-p2-txt)" : "var(--color-p2-dim)";
                    }}
                  >
                    <GroupIcon size={15} strokeWidth={1.75} className="shrink-0" />
                    <span className="truncate">{t(group.rowLabelKey) || group.label}</span>
                    {holdsActive && !open && (
                      <span
                        className="shrink-0"
                        style={{
                          width: 5,
                          height: 5,
                          borderRadius: "999px",
                          backgroundColor: "var(--color-p2-amb)",
                        }}
                      />
                    )}
                    <motion.span
                      className="ml-auto shrink-0 grid place-items-center"
                      animate={{ rotate: open ? 180 : 0 }}
                      transition={{ duration: 0.18, ease: EASE }}
                    >
                      <ChevronDown size={12} />
                    </motion.span>
                  </button>

                  <AnimatePresence initial={false}>
                    {open && (
                      <motion.div
                        initial={{ height: 0, opacity: 0 }}
                        animate={{ height: "auto", opacity: 1 }}
                        exit={{ height: 0, opacity: 0 }}
                        transition={{ duration: 0.2, ease: EASE }}
                        className="overflow-hidden"
                      >
                        <div className="flex flex-col gap-0.5 pt-0.5">
                          {group.children.map((child) => (
                            <Row key={child.href} item={child} nested />
                          ))}
                        </div>
                      </motion.div>
                    )}
                  </AnimatePresence>
                </div>
              );
            })}
      </nav>

      {/* Zone 5 — who am I, is everything running */}
      <SidebarFooter collapsed={sidebarCollapsed} />

      {/* Right-click menu: pin, unpin, reorder */}
      <AnimatePresence>
        {ctx && (
          <motion.div
            initial={{ opacity: 0, scale: 0.96 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.96 }}
            transition={{ duration: 0.12 }}
            className="fixed z-[60] p-1"
            style={{
              left: ctx.x,
              top: ctx.y,
              minWidth: 168,
              backgroundColor: "var(--color-p2-pan2)",
              border: "1px solid var(--color-p2-line)",
              borderRadius: "10px",
              boxShadow: "var(--shadow-elevated)",
            }}
          >
            <CtxItem
              icon={ctx.pinned ? <PinOff size={12} /> : <Pin size={12} />}
              label={ctx.pinned ? t("unpin") : t("pin")}
              onClick={() => togglePin(ctx.href)}
            />
            {ctx.pinned && (
              <>
                <CtxItem
                  icon={<ArrowUp size={12} />}
                  label={t("moveUp")}
                  disabled={pinnedNav.indexOf(ctx.href) === 0}
                  onClick={() => movePin(ctx.href, -1)}
                />
                <CtxItem
                  icon={<ArrowDown size={12} />}
                  label={t("moveDown")}
                  disabled={pinnedNav.indexOf(ctx.href) === pinnedNav.length - 1}
                  onClick={() => movePin(ctx.href, 1)}
                />
              </>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </motion.aside>
  );
}

function CtxItem({
  icon,
  label,
  onClick,
  disabled = false,
}: {
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="w-full flex items-center gap-2.5 cursor-pointer disabled:opacity-35 disabled:cursor-default"
      style={{
        height: 30,
        padding: "0 10px",
        borderRadius: "7px",
        ...MONO,
        fontSize: "11.5px",
        color: "var(--color-p2-txt)",
      }}
      onMouseEnter={(e) => {
        if (!disabled) (e.currentTarget as HTMLElement).style.backgroundColor = "var(--color-p2-pan)";
      }}
      onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.backgroundColor = "transparent")}
    >
      <span className="shrink-0" style={{ color: "var(--color-p2-faint)" }}>
        {icon}
      </span>
      {label}
    </button>
  );
}

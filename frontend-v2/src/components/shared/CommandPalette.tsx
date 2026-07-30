"use client";

import { useCallback } from "react";
import { Command } from "cmdk";
import { useRouter } from "next/navigation";
import { motion, AnimatePresence } from "framer-motion";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Home,
  FolderKanban,
  Bot,
  Inbox,
  Settings,
  Plus,
  CheckCheck,
  Search,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { useAppStore } from "@/lib/store";
import { api } from "@/lib/api";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { EntityIcon } from "@/components/shared/EntityIcon";

// ── v3 styles (Tokens only) ─────────────────────────────────────────────────
// Items: 13px General Sans, ausgewählt = accent-subtle Fläche + 2px Akzent-Balken
// links (inset shadow, eckig) + accent-light Text.
const itemClass =
  "flex items-center gap-3 px-3 py-2 rounded-sm text-[13px] cursor-pointer transition-colors " +
  "data-[selected=true]:bg-[var(--color-accent-subtle)] data-[selected=true]:text-[var(--color-accent-light)] " +
  "data-[selected=true]:shadow-[inset_2px_0_0_0_var(--color-accent)]";

// Gruppen-Header im .label-sys-Stil (Mono 10px uppercase, weit getrackt, muted).
const groupClass =
  "[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 " +
  "[&_[cmdk-group-heading]]:font-mono [&_[cmdk-group-heading]]:text-[10px] " +
  "[&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:uppercase " +
  "[&_[cmdk-group-heading]]:tracking-[0.14em] [&_[cmdk-group-heading]]:text-[var(--color-text-muted)]";

const kbdClass = "font-mono text-[10px] px-1.5 py-0.5 rounded-sm shrink-0";
const kbdStyle = {
  backgroundColor: "var(--color-bg-deep)",
  color: "var(--color-text-muted)",
  border: "1px solid var(--color-border)",
} as const;

export default function CommandPalette() {
  const t = useTranslations("shell");
  const tNav = useTranslations("nav");
  const router = useRouter();
  const qc = useQueryClient();
  const { commandPaletteOpen, setCommandPaletteOpen, activeBoardId } =
    useAppStore();

  // Body-Scroll-Lock: verhindert Hintergrund-Scrolling auf iOS (MOBILE-SPEC M4)
  useBodyScrollLock(commandPaletteOpen);

  const { data: agents } = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.agents.list(),
    enabled: commandPaletteOpen,
  });

  const close = useCallback(
    () => setCommandPaletteOpen(false),
    [setCommandPaletteOpen]
  );

  const navigate = useCallback(
    (href: string) => {
      router.push(href);
      close();
    },
    [router, close]
  );

  const approveAll = useCallback(async () => {
    const approvals = await api.approvals.list();
    await Promise.all(
      approvals.map((a) => api.approvals.resolve(a.id, "approved"))
    );
    qc.invalidateQueries({ queryKey: ["approvals"] });
    close();
  }, [close, qc]);

  return (
    <AnimatePresence>
      {commandPaletteOpen && (
        <>
          {/* Backdrop */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="fixed inset-0 z-50"
            style={{ backgroundColor: "rgba(2, 4, 8, 0.7)" }}
            onClick={close}
          />

          {/* Palette */}
          <motion.div
            initial={{ opacity: 0, scale: 0.96, y: -10 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.96, y: -10 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            className="fixed left-1/2 z-50 w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 rounded-md overflow-hidden corner-ticks"
            style={{
              top: "calc(env(safe-area-inset-top) + 1rem)",
              backgroundColor: "var(--color-bg-elevated)",
              border: "1px solid var(--color-border)",
              boxShadow: "var(--shadow-elevated)",
            }}
          >
            <Command
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  e.preventDefault();
                  close();
                }
              }}
            >
              {/* Search input */}
              <div
                className="flex items-center gap-3 px-4"
                style={{ borderBottom: "1px solid var(--color-border)" }}
              >
                <Search
                  size={15}
                  style={{ color: "var(--color-text-muted)", flexShrink: 0 }}
                />
                <Command.Input
                  autoFocus
                  placeholder={t("searchPlaceholder")}
                  className="flex-1 py-3.5 bg-transparent font-mono text-[13px] outline-none"
                  style={{
                    color: "var(--color-text-primary)",
                    caretColor: "var(--color-accent)",
                  }}
                />
                <kbd className={kbdClass} style={kbdStyle}>
                  Esc
                </kbd>
              </div>

              {/* Results */}
              <Command.List
                className="max-h-[60dvh] sm:max-h-80 overflow-y-auto p-1.5"
                style={{ color: "var(--color-text-primary)" }}
              >
                <Command.Empty
                  className="py-8 text-center text-sm"
                  style={{ color: "var(--color-text-muted)" }}
                >
                  {t("noResults")}
                </Command.Empty>

                {/* Navigation */}
                <Command.Group heading={t("navigation")} className={groupClass}>
                  {[
                    { icon: Home, label: tNav("home"), href: "/" },
                    { icon: FolderKanban, label: tNav("tasks"), href: "/tasks" },
                    { icon: Bot, label: tNav("agents"), href: "/agents" },
                    { icon: Inbox, label: tNav("inbox"), href: "/inbox" },
                    {
                      icon: Settings,
                      label: tNav("settings"),
                      href: "/settings",
                    },
                  ].map(({ icon: Icon, label, href }) => (
                    <Command.Item
                      key={href}
                      value={`go ${label}`}
                      onSelect={() => navigate(href)}
                      className={itemClass}
                    >
                      <Icon
                        size={15}
                        style={{ color: "var(--color-text-secondary)" }}
                      />
                      {label}
                    </Command.Item>
                  ))}
                </Command.Group>

                {/* Quick Actions */}
                <Command.Group heading={t("actions")} className={groupClass}>
                  <Command.Item
                    value="new task"
                    onSelect={() => navigate("/tasks")}
                    className={itemClass}
                  >
                    <Plus
                      size={15}
                      style={{ color: "var(--color-info)" }}
                    />
                    {t("newTask")}
                    <kbd
                      className={`ml-auto ${kbdClass}`}
                      style={kbdStyle}
                    >
                      Cmd+N
                    </kbd>
                  </Command.Item>
                  <Command.Item
                    value="approve all"
                    onSelect={approveAll}
                    className={itemClass}
                  >
                    <CheckCheck
                      size={15}
                      style={{ color: "var(--color-online)" }}
                    />
                    {t("approveAll")}
                    <kbd
                      className={`ml-auto ${kbdClass}`}
                      style={kbdStyle}
                    >
                      Cmd+Shift+A
                    </kbd>
                  </Command.Item>
                </Command.Group>

                {/* Agents */}
                {agents && agents.length > 0 && (
                  <Command.Group heading={tNav("agents")} className={groupClass}>
                    {agents.map((agent) => (
                      <Command.Item
                        key={agent.id}
                        value={`agent ${agent.name}`}
                        onSelect={() => navigate(`/agents/${agent.id}`)}
                        className={itemClass}
                      >
                        <span className="text-xs">
                          <EntityIcon value={agent.emoji} size={14} />
                        </span>
                        {agent.name}
                        <span
                          className="ml-auto font-mono text-[10px] capitalize"
                          style={{ color: "var(--color-text-muted)" }}
                        >
                          {agent.status}
                        </span>
                      </Command.Item>
                    ))}
                  </Command.Group>
                )}
              </Command.List>
            </Command>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}

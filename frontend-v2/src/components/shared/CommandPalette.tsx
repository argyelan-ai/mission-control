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
import { useAppStore } from "@/lib/store";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";

export default function CommandPalette() {
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
            style={{ backgroundColor: "rgba(2, 2, 3, 0.7)" }}
            onClick={close}
          />

          {/* Palette */}
          <motion.div
            initial={{ opacity: 0, scale: 0.96, y: -10 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.96, y: -10 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            className="fixed left-1/2 z-50 w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 rounded-xl overflow-hidden"
            style={{
              top: "calc(env(safe-area-inset-top) + 1rem)",
              backgroundColor: "var(--color-bg-elevated)",
              border: "1px solid var(--color-border-strong)",
              boxShadow:
                "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
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
                  placeholder="Suche oder Befehl..."
                  className="flex-1 py-3.5 bg-transparent text-sm outline-none"
                  style={{
                    color: "var(--color-text-primary)",
                    caretColor: "var(--color-accent)",
                  }}
                />
                <kbd
                  className="text-[10px] px-1.5 py-0.5 rounded font-mono shrink-0"
                  style={{
                    backgroundColor: "rgba(255, 255, 255, 0.04)",
                    color: "var(--color-text-muted)",
                    border: "1px solid var(--color-border)",
                  }}
                >
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
                  Keine Ergebnisse
                </Command.Empty>

                {/* Navigation */}
                <Command.Group
                  heading="Navigation"
                  className="[&_[cmdk-group-heading]]:text-nav [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:px-2"
                >
                  {[
                    { icon: Home, label: "Home", href: "/" },
                    { icon: FolderKanban, label: "Tasks", href: "/tasks" },
                    { icon: Bot, label: "Agents", href: "/agents" },
                    { icon: Inbox, label: "Inbox", href: "/inbox" },
                    {
                      icon: Settings,
                      label: "Einstellungen",
                      href: "/settings",
                    },
                  ].map(({ icon: Icon, label, href }) => (
                    <Command.Item
                      key={href}
                      value={`go ${label}`}
                      onSelect={() => navigate(href)}
                      className="flex items-center gap-3 px-3 py-2 rounded-lg text-sm cursor-pointer transition-colors"
                      style={
                        {
                          "--item-bg": C.accentSubtle,
                        } as React.CSSProperties
                      }
                      data-selected-bg="true"
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
                <Command.Group
                  heading="Aktionen"
                  className="[&_[cmdk-group-heading]]:text-nav [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:px-2"
                >
                  <Command.Item
                    value="new task neuer task"
                    onSelect={() => navigate("/tasks")}
                    className="flex items-center gap-3 px-3 py-2 rounded-lg text-sm cursor-pointer"
                  >
                    <Plus
                      size={15}
                      style={{ color: "var(--color-info)" }}
                    />
                    Neuer Task
                    <kbd
                      className="ml-auto text-[10px] font-mono"
                      style={{ color: "var(--color-text-muted)" }}
                    >
                      Cmd+N
                    </kbd>
                  </Command.Item>
                  <Command.Item
                    value="approve all alle genehmigen"
                    onSelect={approveAll}
                    className="flex items-center gap-3 px-3 py-2 rounded-lg text-sm cursor-pointer"
                  >
                    <CheckCheck
                      size={15}
                      style={{ color: "var(--color-online)" }}
                    />
                    Alle Approvals genehmigen
                    <kbd
                      className="ml-auto text-[10px] font-mono"
                      style={{ color: "var(--color-text-muted)" }}
                    >
                      Cmd+Shift+A
                    </kbd>
                  </Command.Item>
                </Command.Group>

                {/* Agents */}
                {agents && agents.length > 0 && (
                  <Command.Group
                    heading="Agents"
                    className="[&_[cmdk-group-heading]]:text-nav [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:px-2"
                  >
                    {agents.map((agent) => (
                      <Command.Item
                        key={agent.id}
                        value={`agent ${agent.name}`}
                        onSelect={() => navigate(`/agents/${agent.id}`)}
                        className="flex items-center gap-3 px-3 py-2 rounded-lg text-sm cursor-pointer"
                      >
                        <span className="text-xs">
                          {agent.emoji ?? ""}
                        </span>
                        {agent.name}
                        <span
                          className="ml-auto text-xs capitalize"
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

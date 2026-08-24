"use client";

import { useRouter } from "next/navigation";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { Settings, LogOut, PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { useAppStore } from "@/lib/store";
import { api, clearToken } from "@/lib/api";

const MONO = { fontFamily: "var(--font-p2-mono)" };

/**
 * SidebarFooter — Shell v4 zone 5. Replaces the full-width cyan StatusBar:
 * one dot on the avatar for system health (plain words in its tooltip),
 * plus the user and the shell's controls.
 * Same data source as the old bar (system.status), a fraction of the noise.
 */
export default function SidebarFooter({ collapsed = false }: { collapsed?: boolean }) {
  const t = useTranslations("shell");
  const tNav = useTranslations("nav");
  const router = useRouter();
  const { currentUser, sidebarCollapsed, toggleSidebar } = useAppStore();

  const { data: status, isError } = useQuery({
    queryKey: ["system-status"],
    queryFn: api.system.status,
    refetchInterval: 30_000,
  });

  // Gateway was retired (ADR-039) — health now reflects the core dependencies.
  const dbOk = status?.components?.database?.status === "ok";
  const redisOk = status?.components?.redis?.status === "ok";

  let tone: "ok" | "warn" | "err";
  let health: string;
  if (isError || (status && !dbOk && !redisOk)) {
    tone = "err";
    health = t("systemNoContact");
  } else if (!status) {
    tone = "warn";
    health = t("systemChecking");
  } else if (dbOk && redisOk) {
    tone = "ok";
    health = t("systemOk");
  } else {
    tone = "warn";
    health = !dbOk ? t("systemDbDegraded") : t("systemRedisDegraded");
  }

  const toneColor =
    tone === "ok"
      ? "var(--color-p2-ok)"
      : tone === "warn"
        ? "var(--color-p2-wrn)"
        : "var(--color-p2-err)";

  const initial = (currentUser?.name?.[0] ?? "?").toUpperCase();

  function handleLogout() {
    clearToken();
    router.replace("/login");
  }

  if (collapsed) {
    return (
      <div
        className="mt-auto shrink-0 flex flex-col items-center gap-2.5 pt-2.5"
        style={{ borderTop: "1px solid var(--color-p2-line2)" }}
      >
        <button
          onClick={toggleSidebar}
          aria-label={t("expandSidebar")}
          title={`${t("expandSidebar")} · ⌘B`}
          className="grid place-items-center cursor-pointer"
          style={{ width: 30, height: 30, borderRadius: "var(--radius-full)", color: "var(--color-p2-dim)" }}
        >
          <PanelLeftOpen size={14} />
        </button>
        <span
          title={health}
          style={{ width: 7, height: 7, borderRadius: "var(--radius-full)", backgroundColor: toneColor }}
        />
        <Link
          href="/settings"
          aria-label={t("settings")}
          className="grid place-items-center"
          style={{ width: 30, height: 30, borderRadius: "var(--radius-full)", color: "var(--color-p2-dim)" }}
        >
          <Settings size={14} />
        </Link>
        <div
          title={currentUser?.name ?? ""}
          className="grid place-items-center"
          style={{
            width: 28,
            height: 28,
            borderRadius: "var(--radius-full)",
            backgroundColor: "var(--color-p2-amb)",
            color: "var(--color-p2-inv)",
            fontFamily: "var(--font-p2-mono)",
            fontWeight: 700,
            fontSize: "12.5px",
          }}
        >
          {initial}
        </div>
        <button
          onClick={handleLogout}
          aria-label={tNav("logout")}
          className="grid place-items-center cursor-pointer mb-1"
          style={{ width: 30, height: 30, borderRadius: "var(--radius-full)", color: "var(--color-p2-faint)" }}
        >
          <LogOut size={13} />
        </button>
      </div>
    );
  }

  return (
    <div
      className="mt-auto shrink-0 flex items-center gap-2 pt-2.5 px-1"
      style={{ borderTop: "1px solid var(--color-p2-line2)" }}
    >
      <div className="relative shrink-0" title={health}>
        <div
          className="grid place-items-center"
          style={{
            width: 30,
            height: 30,
            borderRadius: "var(--radius-full)",
            backgroundColor: "var(--color-p2-amb)",
            color: "var(--color-p2-inv)",
            fontFamily: "var(--font-p2-mono)",
            fontWeight: 700,
            fontSize: "12.5px",
          }}
        >
          {initial}
        </div>
        {/* System health rides the avatar, so the line below keeps its width */}
        <span
          aria-hidden
          className="absolute"
          style={{
            right: -1,
            bottom: -1,
            width: 9,
            height: 9,
            borderRadius: "var(--radius-full)",
            backgroundColor: toneColor,
            border: "2px solid var(--color-p2-pan)",
          }}
        />
      </div>

      {/* Name only. System health is the dot on the avatar — the spelled-out
          line under the name was noise the operator read once and never again. */}
      <div
        className="min-w-0 flex-1 truncate"
        style={{ ...MONO, fontSize: "11.5px", fontWeight: 700, color: "var(--color-p2-txt)" }}
      >
        {currentUser?.name ?? "—"}
      </div>

      <button
        onClick={toggleSidebar}
        aria-label={t("collapseSidebar")}
        title={`${t("collapseSidebar")} · ⌘B`}
        className="grid place-items-center shrink-0 cursor-pointer"
        style={{ width: 26, height: 26, borderRadius: "var(--radius-full)", color: "var(--color-p2-faint)" }}
        onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.color = "var(--color-p2-txt)")}
        onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.color = "var(--color-p2-faint)")}
      >
        {sidebarCollapsed ? <PanelLeftOpen size={14} /> : <PanelLeftClose size={14} />}
      </button>
      <Link
        href="/settings"
        aria-label={t("settings")}
        className="grid place-items-center shrink-0"
        style={{ width: 26, height: 26, borderRadius: "var(--radius-full)", color: "var(--color-p2-faint)" }}
        onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.color = "var(--color-p2-txt)")}
        onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.color = "var(--color-p2-faint)")}
      >
        <Settings size={14} />
      </Link>
      <button
        onClick={handleLogout}
        aria-label={tNav("logout")}
        className="grid place-items-center shrink-0 cursor-pointer"
        style={{ width: 26, height: 26, borderRadius: "var(--radius-full)", color: "var(--color-p2-faint)" }}
        onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.color = "var(--color-p2-err)")}
        onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.color = "var(--color-p2-faint)")}
      >
        <LogOut size={13} />
      </button>
    </div>
  );
}

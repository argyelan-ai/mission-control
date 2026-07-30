"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useAppStore } from "@/lib/store";
import { api } from "@/lib/api";

/**
 * StatusBar — P2 „SIGNAL" (ui-redesign-v3).
 * Die Instrumentenleiste im htop-Stil: SYS · AGT · BRD · Clock · ⌘K.
 *
 * System A: die Leiste war früher eine volle Akzent-Fläche (erst Cyan, dann
 * Bone) mit dunkler Tinte. Ein heller Vollflächen-Balken macht das Rahmenwerk
 * zum lautesten Element der Seite — Betonung muss aber Bedeutung tragen.
 * Jetzt: dunkle Chrome-Fläche wie die übrige Shell, neutral heller Text,
 * Trennung über eine Hairline. Farbe trägt nur noch der SYS-Punkt
 * (online/error) — das ist die einzige Information hier, die einen Zustand hat.
 */
export default function StatusBar() {
  const t = useTranslations("shell");
  const { setCommandPaletteOpen, activeBoardId, boards } = useAppStore();

  const { data: status } = useQuery({
    queryKey: ["system-status"],
    queryFn: api.system.status,
    refetchInterval: 30_000,
  });

  const { data: metrics } = useQuery({
    queryKey: ["system-metrics"],
    queryFn: api.system.metrics,
    refetchInterval: 30_000,
  });

  // Clock — instrument tick, updates every second
  const [now, setNow] = useState<Date | null>(null);
  useEffect(() => {
    setNow(new Date());
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  // Gateway was retired (Phase 29 / ADR-039). Connection now reflects core deps.
  const dbOk = status?.components?.database?.status === "ok";
  const redisOk = status?.components?.redis?.status === "ok";
  const connected = !!status && dbOk && redisOk;
  const agentsOnline = metrics?.agents?.online ?? 0;
  const agentsTotal = metrics?.agents?.total ?? 0;

  const activeBoard = boards.find((b) => b.id === activeBoardId);

  const sep = (
    <span aria-hidden style={{ color: "var(--color-p2-faint)" }}>
      |
    </span>
  );

  return (
    <div
      className="hidden md:flex items-center justify-between px-4 shrink-0"
      style={{
        height: "30px",
        // Chrome, kein Banner: dunkle Fläche + Hairline statt Helligkeitssprung.
        backgroundColor: "var(--color-p2-pan)",
        borderBottom: "1px solid var(--color-p2-line)",
        color: "var(--color-p2-txt)", // #EEEEEE auf #171717 = 15.5:1
        fontFamily: "var(--font-p2-mono)",
        fontWeight: 700,
        fontSize: "10.5px",
        letterSpacing: "0.05em",
      }}
    >
      {/* Left: telemetry datastream */}
      <div className="flex items-center gap-2.5">
        <span className="flex items-center gap-1.5">
          {/* Die einzige Farbe in der Leiste — sie trägt einen Zustand. */}
          <span
            className="w-1.5 h-1.5"
            style={{
              backgroundColor: connected
                ? "var(--color-p2-ok)"
                : "var(--color-p2-err)",
            }}
          />
          <span>{connected ? "SYS OK" : "SYS OFFLINE"}</span>
        </span>
        {sep}
        <span>
          AGT {agentsOnline}/{agentsTotal}
        </span>
        {activeBoard && (
          <>
            {sep}
            <span>BRD {activeBoard.name.toUpperCase()}</span>
          </>
        )}
        {now && (
          <>
            {sep}
            <span suppressHydrationWarning style={{ color: "var(--color-p2-dim)" }}>
              {now.toLocaleTimeString("de-CH", { hour12: false })}
            </span>
          </>
        )}
      </div>

      {/* Right: command palette */}
      <button
        onClick={() => setCommandPaletteOpen(true)}
        className="flex items-center gap-1.5 cursor-pointer hover:opacity-70 transition-opacity uppercase"
        style={{ color: "var(--color-p2-txt)", letterSpacing: "0.05em", fontWeight: 700 }}
        aria-label={t("openCommandPalette")}
      >
        <kbd
          className="px-1.5 py-0.5"
          style={{
            border: "1px solid var(--color-p2-line)",
            fontFamily: "var(--font-p2-mono)",
            fontSize: "10px",
          }}
        >
          ⌘K
        </kbd>
        <span>PALETTE</span>
      </button>
    </div>
  );
}

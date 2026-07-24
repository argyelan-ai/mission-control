"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useAppStore } from "@/lib/store";
import { api } from "@/lib/api";

/**
 * StatusBar — P2 „PHOSPHOR+ CYAN" (ui-redesign-v3).
 * Die Signatur-Instrumentenleiste: volle Cyan-Fläche, dunkler Text, htop-Stil.
 * Gleiche Daten wie v3 (SYS · AGT · BRD · Clock · ⌘K) — nur neue Oberfläche.
 */
export default function StatusBar() {
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

  // On-cyan inks: readable dark variants of status hues (reverse-video logic)
  const ink = "var(--color-p2-inv)";
  const okInk = "#0E3A1C";
  const errInk = "#7A1A0E";

  const sep = (
    <span aria-hidden style={{ opacity: 0.45 }}>
      |
    </span>
  );

  return (
    <div
      className="hidden md:flex items-center justify-between px-4 shrink-0"
      style={{
        height: "30px",
        backgroundColor: "var(--color-p2-amb)",
        color: ink,
        fontFamily: "var(--font-p2-mono)",
        fontWeight: 700,
        fontSize: "10.5px",
        letterSpacing: "0.05em",
      }}
    >
      {/* Left: telemetry datastream */}
      <div className="flex items-center gap-2.5">
        <span className="flex items-center gap-1.5">
          <span
            className="w-1.5 h-1.5"
            style={{ backgroundColor: connected ? okInk : errInk }}
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
            <span suppressHydrationWarning style={{ opacity: 0.65 }}>
              {now.toLocaleTimeString("de-CH", { hour12: false })}
            </span>
          </>
        )}
      </div>

      {/* Right: command palette */}
      <button
        onClick={() => setCommandPaletteOpen(true)}
        className="flex items-center gap-1.5 cursor-pointer hover:opacity-70 transition-opacity uppercase"
        style={{ color: ink, letterSpacing: "0.05em", fontWeight: 700 }}
        aria-label="Open command palette"
      >
        <kbd
          className="px-1.5 py-0.5"
          style={{
            border: "1px solid var(--color-p2-inv)",
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

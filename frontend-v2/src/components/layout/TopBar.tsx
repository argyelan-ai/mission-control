"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { useAppStore } from "@/lib/store";
import { api } from "@/lib/api";
import { channelFor } from "./channel";

/**
 * TopBar — P2 „PHOSPHOR+ CYAN" desktop chrome (ui-redesign-v3).
 * Slim instrument strip above the content column: brand + channel id (CH) +
 * page name left, board / agents / local clock right. Display-only — board
 * switching stays in the WorkspaceSwitcher rail, palette stays on ⌘K.
 */

export default function TopBar() {
  const pathname = usePathname();
  const { activeBoardId, boards } = useAppStore();
  const { ch, page } = channelFor(pathname);

  const { data: metrics } = useQuery({
    queryKey: ["system-metrics"],
    queryFn: api.system.metrics,
    refetchInterval: 30_000,
  });
  const agentsOnline = metrics?.agents?.online ?? 0;
  const agentsTotal = metrics?.agents?.total ?? 0;
  const activeBoard = boards.find((b) => b.id === activeBoardId);

  // UTC clock — same instrument tick as StatusBar
  const [now, setNow] = useState<Date | null>(null);
  useEffect(() => {
    setNow(new Date());
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  return (
    <div
      className="hidden md:flex items-center shrink-0 px-4"
      style={{
        height: "38px",
        backgroundColor: "rgba(8,7,5,0.92)",
        borderBottom: "1px solid var(--color-p2-line2)",
        fontFamily: "var(--font-p2-mono)",
        fontSize: "11px",
        color: "var(--color-p2-dim)",
      }}
    >
      <span
        style={{
          fontFamily: "var(--font-p2-display)",
          fontWeight: 700,
          fontSize: "12.5px",
          color: "var(--color-p2-txt)",
          letterSpacing: "0.02em",
        }}
      >
        MC<span style={{ color: "var(--color-p2-amb)" }}>/</span>OS
      </span>
      <span
        className="ml-4"
        style={{
          color: "var(--color-p2-amb)",
          border: "1px solid var(--color-p2-amb-d)",
          padding: "2px 7px",
          fontSize: "10px",
          fontWeight: 700,
          letterSpacing: "0.08em",
        }}
      >
        {ch}
      </span>
      <span className="ml-3" style={{ letterSpacing: "0.1em" }}>
        {page}
      </span>

      <div className="ml-auto flex items-center gap-4" style={{ letterSpacing: "0.06em" }}>
        {activeBoard && (
          <span>
            BRD:{" "}
            <span style={{ color: "var(--color-p2-txt)" }}>
              {activeBoard.name.toUpperCase()}
            </span>
          </span>
        )}
        <span>
          AGT{" "}
          <span style={{ color: "var(--color-p2-txt)" }}>
            {agentsOnline}/{agentsTotal}
          </span>
        </span>
        {now && (
          <span suppressHydrationWarning style={{ color: "var(--color-p2-txt)" }}>
            {now.toLocaleTimeString("de-CH", { hour12: false })}
          </span>
        )}
      </div>
    </div>
  );
}

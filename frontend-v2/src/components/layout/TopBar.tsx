"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { useAppStore } from "@/lib/store";
import { api } from "@/lib/api";
import { pageNameFor } from "./channel";

/**
 * TopBar — P2 „SIGNAL" desktop chrome (ui-redesign-v3).
 * Slim instrument strip above the content column: page name left, board /
 * agents / local clock right. Display-only — board
 * switching stays in the WorkspaceSwitcher rail, palette stays on ⌘K.
 */

export default function TopBar() {
  const pathname = usePathname();
  const { activeBoardId, boards } = useAppStore();
  const page = pageNameFor(pathname);

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
        backgroundColor: "rgba(10,10,10,0.92)",
        borderBottom: "1px solid var(--color-p2-line2)",
        fontFamily: "var(--font-p2-mono)",
        fontSize: "11px",
        color: "var(--color-p2-dim)",
      }}
    >
      <span style={{ letterSpacing: "0.1em", color: "var(--color-p2-txt)" }}>
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

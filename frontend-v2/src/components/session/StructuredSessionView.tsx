"use client";

import { useState, useEffect, useRef } from "react";
import { api } from "@/lib/api";
import type { CliGlobalSession } from "@/lib/types";
import { Terminal, ChevronDown, ChevronRight, Zap, Radio, CheckCircle2 } from "lucide-react";
import { C } from "@/lib/colors";

export type EventKind = "sys" | "tool" | "out" | "status" | "log";

export interface ParsedEvent {
  kind: EventKind;
  label: string;
  raw: string;
  body: string | null;
  ts: number;
}

export function parseLine(line: string): ParsedEvent | null {
  const trimmed = line.trim();
  if (!trimmed) return null;

  const toolMatch = trimmed.match(/^\[TOOL\]\s*(\S+)\s*([\s\S]*)/);
  if (toolMatch) {
    return { kind: "tool", label: toolMatch[1], raw: line, body: toolMatch[2] || null, ts: Date.now() };
  }

  const outMatch = trimmed.match(/^\[OUT\]\s*([\s\S]*)/);
  if (outMatch) {
    return { kind: "out", label: "Output", raw: line, body: outMatch[1] || null, ts: Date.now() };
  }

  const sysMatch = trimmed.match(/^\[SYS\]\s*([\s\S]*)/);
  if (sysMatch) {
    return { kind: "sys", label: "System", raw: line, body: sysMatch[1] || null, ts: Date.now() };
  }

  if (trimmed.startsWith("✓") || trimmed.startsWith("✗")) {
    return { kind: "status", label: trimmed.startsWith("✓") ? "Done" : "Error", raw: line, body: trimmed, ts: Date.now() };
  }

  if (trimmed.match(/^\[\d{4}-\d{2}-\d{2}T/)) {
    return { kind: "log", label: "Log", raw: line, body: trimmed, ts: Date.now() };
  }

  return null;
}

// Removes ANSI escape codes from terminal output
function stripAnsi(str: string): string {
  // eslint-disable-next-line no-control-regex
  return str.replace(/\x1b\[[0-9;?]*[a-zA-Z]/g, "").replace(/\x1b\][^\x07]*\x07/g, "");
}

export function useStructuredStream(selected: CliGlobalSession | null): (ParsedEvent & { id: number })[] {
  const [events, setEvents] = useState<(ParsedEvent & { id: number })[]>([]);
  const bufRef = useRef("");
  const wsRef = useRef<WebSocket | null>(null);
  const idRef = useRef(0);

  useEffect(() => {
    setEvents([]);
    bufRef.current = "";
    idRef.current = 0;
    if (wsRef.current) { wsRef.current.close(1000); wsRef.current = null; }
    if (!selected || !selected.agent_id) return;

    const url = api.cliSessions.wsUrl(selected.agent_id, selected.shell);
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onmessage = (evt) => {
      const chunk = evt.data instanceof ArrayBuffer
        ? new TextDecoder().decode(new Uint8Array(evt.data))
        : (evt.data as string);

      bufRef.current += stripAnsi(chunk);

      const lines = bufRef.current.split("\n");
      bufRef.current = lines.pop() ?? "";

      const newEvents: (ParsedEvent & { id: number })[] = [];
      for (const line of lines) {
        const ev = parseLine(line);
        if (ev) newEvents.push({ ...ev, id: idRef.current++ });
      }
      if (newEvents.length > 0) {
        setEvents((prev) => [...prev, ...newEvents].slice(-200));
      }
    };

    return () => { ws.close(1000); wsRef.current = null; };
  }, [selected?.session]);

  return events;
}

// ── Event Card ────────────────────────────────────────────────────────────────

const KIND_CONFIG = {
  tool:   { color: C.accent,   bg: C.accentSubtle,        icon: <Terminal size={11} />,     label: "Tool" },
  out:    { color: C.online,   bg: `${C.online}0F`,       icon: <Radio size={11} />,        label: "Output" },
  sys:    { color: C.info,     bg: `${C.info}0F`,         icon: <Zap size={11} />,          label: "Sys" },
  status: { color: C.warning,  bg: `${C.warning}14`,      icon: <CheckCircle2 size={11} />, label: "Status" },
  log:    { color: C.textMuted, bg: "transparent",        icon: null,                        label: "" },
} as const;

function EventCard({ event }: { event: ParsedEvent & { id: number } }) {
  const [open, setOpen] = useState(event.kind !== "tool");
  const cfg = KIND_CONFIG[event.kind] ?? KIND_CONFIG.log;

  let bodyContent = event.body ?? "";
  let isJson = false;
  if (event.body) {
    try {
      bodyContent = JSON.stringify(JSON.parse(event.body), null, 2);
      isJson = true;
    } catch { /* plain text */ }
  }

  if (event.kind === "log") {
    return (
      <div className="px-3 py-0.5 text-[10px] font-mono" style={{ color: C.textMuted }}>
        {event.body}
      </div>
    );
  }

  return (
    <div
      className="mx-2 my-0.5 rounded-lg overflow-hidden"
      style={{ background: cfg.bg, border: `1px solid ${cfg.color}22` }}
    >
      <button
        className="w-full flex items-center gap-2 px-3 py-1.5 cursor-pointer text-left"
        onClick={() => setOpen((v) => !v)}
      >
        <span style={{ color: cfg.color }}>{cfg.icon}</span>
        <span className="text-[10px] font-semibold font-mono" style={{ color: cfg.color }}>
          {event.kind === "tool" ? event.label : cfg.label}
        </span>
        {event.kind === "tool" && !open && event.body && (
          <span className="text-[10px] font-mono truncate flex-1" style={{ color: C.textMuted }}>
            {event.body.slice(0, 80)}
          </span>
        )}
        <span className="ml-auto" style={{ color: C.textMuted }}>
          {open ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
        </span>
      </button>

      {open && event.body && (
        <div className="px-3 pb-2">
          <pre
            className="text-[10px] font-mono whitespace-pre-wrap break-all"
            style={{ color: isJson ? C.textPrimary : C.textSecondary, maxHeight: 300, overflow: "auto" }}
            tabIndex={0}
            role="region"
            aria-label="Event body"
          >
            {bodyContent}
          </pre>
        </div>
      )}
    </div>
  );
}

// ── Structured Session View ───────────────────────────────────────────────────

export function StructuredSessionView({ selected }: { selected: CliGlobalSession }) {
  const events = useStructuredStream(selected);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events.length]);

  if (events.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <p className="text-[11px]" style={{ color: C.textMuted }}>Warte auf Agent-Output…</p>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto py-2" tabIndex={0} role="region" aria-label="Session events">
      {events.map((ev) => (
        <EventCard key={ev.id} event={ev} />
      ))}
      <div ref={bottomRef} />
    </div>
  );
}

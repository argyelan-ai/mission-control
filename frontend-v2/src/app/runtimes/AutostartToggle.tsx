"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Power } from "lucide-react";
import { api } from "@/lib/api";
import { C, STATUS, STATUS_TEXT } from "@/lib/colors";

/**
 * Engine Control v0 (ADR-057) — per-runtime "Autostart bei Boot" toggle.
 *
 * Flips a flag file on the runtime's bound host over SSH (backend touches/
 * removes it, then reads it back to confirm). Three states, never optimistic:
 * on / off / unknown (host unreachable — disabled, with a tooltip explaining
 * why). Only rendered for runtimes with autostart_supported=true.
 */
export function AutostartToggle({ slug }: { slug: string }) {
  const queryClient = useQueryClient();

  const { data: status, isLoading } = useQuery({
    queryKey: ["runtime-autostart", slug],
    queryFn: () => api.runtimes.db.autostartStatus(slug),
    staleTime: 15_000,
  });

  const mutation = useMutation({
    mutationFn: (enabled: boolean) => api.runtimes.db.setAutostart(slug, enabled),
    onSuccess: (data) => {
      queryClient.setQueryData(["runtime-autostart", slug], data);
    },
  });

  const unknown = !isLoading && (status == null || status.reachable === false);
  const enabled = status?.enabled === true;
  const busy = isLoading || mutation.isPending;

  const title = unknown
    ? "Host nicht erreichbar — Autostart-Status unbekannt"
    : enabled
      ? "Autostart bei Boot: an — klicken zum Deaktivieren"
      : "Autostart bei Boot: aus — klicken zum Aktivieren";

  return (
    <button
      type="button"
      role="switch"
      aria-checked={enabled}
      aria-label="Autostart bei Boot"
      title={title}
      disabled={unknown || busy}
      onClick={() => mutation.mutate(!enabled)}
      className="flex items-center gap-1.5 shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium transition-opacity"
      style={{
        border: `1px solid ${unknown ? C.borderSubtle : enabled ? STATUS.online : C.borderSubtle}`,
        color: unknown ? C.textDim : enabled ? STATUS_TEXT.online : C.textMuted,
        background: enabled && !unknown ? C.accentSubtle : "transparent",
        cursor: unknown || busy ? "not-allowed" : "pointer",
        opacity: busy ? 0.6 : 1,
      }}
    >
      {busy ? (
        <Loader2 size={10} className="animate-spin" />
      ) : (
        <Power size={10} />
      )}
      {unknown ? "Autostart: unbekannt" : enabled ? "Autostart: an" : "Autostart: aus"}
    </button>
  );
}

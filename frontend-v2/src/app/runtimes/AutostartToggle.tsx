"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Power } from "lucide-react";
import { useTranslations } from "next-intl";
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
  const t = useTranslations("runtimes.autostart");
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
    ? t("titleUnknown")
    : enabled
      ? t("titleOn")
      : t("titleOff");

  return (
    <button
      type="button"
      role="switch"
      aria-checked={enabled}
      aria-label={t("ariaLabel")}
      title={title}
      disabled={unknown || busy}
      onClick={() => mutation.mutate(!enabled)}
      // Same box as MetaChip so the row reads as one chip family; it just
      // happens to be interactive. min-h-6 keeps it at the WCAG 2.2 SC 2.5.8
      // minimum of 24px.
      className="shrink-0 inline-flex items-center gap-1 label-sys rounded-sm px-1.5 py-0.5 min-h-6 leading-none transition-opacity"
      style={{
        border: `1px solid ${unknown ? C.borderActive : enabled ? `${STATUS.online}40` : C.borderActive}`,
        color: unknown ? C.textDim : enabled ? STATUS_TEXT.online : C.textMuted,
        background: "transparent",
        cursor: unknown || busy ? "not-allowed" : "pointer",
        opacity: busy ? 0.6 : 1,
      }}
    >
      {busy ? (
        <Loader2 size={10} className="animate-spin" />
      ) : (
        <Power size={10} />
      )}
      {unknown ? t("statusUnknown") : enabled ? t("statusOn") : t("statusOff")}
    </button>
  );
}

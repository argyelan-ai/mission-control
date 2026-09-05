"use client";

/**
 * HostAutostartRow — Marks Ein/Aus-Schalter je Box (Rezept-Umschalter P3).
 *
 * Genau EIN Mechanismus: steht der Schalter auf „an", zieht MC nach einem
 * Ausfall oder Neustart das zuletzt hier gestartete Rezept wieder hoch — Solo
 * wie Duo. Steht er auf „aus", startet MC von sich aus gar nichts (das ersetzt
 * den alten Trick, `runtimes.enabled` auf false zu setzen).
 *
 * Regeln aus dem Vertrag (§4) und dem Muster von AutostartToggle.tsx:
 *   - drei Zustände: an / aus / unbekannt — nie geraten
 *   - KEIN Optimistic-Update: erst wenn der PUT geantwortet hat, wechselt die
 *     Anzeige. Ein Schalter, der „an" zeigt, obwohl der Server ihn abgelehnt
 *     hat, wäre eine Lüge
 *   - Fehler bleiben als Satz stehen, nicht als Toast
 *   - Worker-Box (`via_head`): kein Schalter, sondern ein Chip — den Autostart
 *     entscheidet der Kopf, denn die Instanz hängt dort
 *   - schalten dürfen nur Admins (gleiches Gate wie der Geräte-Streifen)
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Power } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { humanApiError } from "@/components/shared/HostRecipeSwitcher";
import { api } from "@/lib/api";
import { C, STATUS, STATUS_TEXT } from "@/lib/colors";
import { useAppStore } from "@/lib/store";

/** Ein Schlüssel für alle Leser dieser Box (Kachel + spätere Panels). */
export const hostAutostartKey = (hostId: string) => ["hosts", hostId, "autostart"] as const;

export function HostAutostartRow({ hostId }: { hostId: string }) {
  const t = useTranslations("runtimes.hostAutostart");
  const locale = useLocale();
  const currentUser = useAppStore((s) => s.currentUser);
  const isAdmin = currentUser?.role === "admin";
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const statusQuery = useQuery({
    queryKey: hostAutostartKey(hostId),
    queryFn: () => api.hosts.autostart(hostId),
    staleTime: 15_000,
    retry: false,
  });
  const status = statusQuery.data ?? null;

  const mutation = useMutation({
    // recipe_slug bleibt, was es war — dieser Schalter entscheidet nur, OB
    // gestartet wird. Welches Rezept gemerkt ist, setzt der Umschalter.
    mutationFn: (enabled: boolean) =>
      api.hosts.setAutostart(hostId, {
        enabled,
        ...(status?.recipe_slug ? { recipe_slug: status.recipe_slug } : {}),
      }),
    onMutate: () => setError(null),
    onSuccess: (next) => {
      queryClient.setQueryData(hostAutostartKey(hostId), next);
      // Die Hosts-Liste trägt autostart_enabled/-recipe_slug mit.
      queryClient.invalidateQueries({ queryKey: ["hosts"], exact: true });
    },
    onError: (err: Error) => setError(t("saveFailed", { message: humanApiError(err) })),
  });

  // Diese Box ist Worker einer Instanz: der Kopf entscheidet. Ein Schalter hier
  // würde eine Zuständigkeit behaupten, die diese Box nicht hat.
  if (status?.via_head) {
    return (
      <Strip>
        <span
          data-testid="host-autostart-via-head"
          className="inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-[10px] font-medium"
          style={{ color: C.textSecondary, border: `1px solid ${C.borderActive}` }}
        >
          <Power size={10} aria-hidden />
          {t("viaHead", { slug: status.via_head.slug })}
        </span>
        <span className="text-[11px]" style={{ color: C.textMuted }}>{t("viaHeadHint")}</span>
      </Strip>
    );
  }

  const loading = statusQuery.isLoading;
  const unknown = !loading && (status == null || statusQuery.isError);
  const enabled = status?.enabled === true;
  const busy = loading || mutation.isPending;
  const recipeName = status?.recipe_display_name ?? status?.recipe_slug ?? null;

  const subtitle = unknown
    ? t("subtitleUnknown")
    : enabled
      ? recipeName
        ? t("subtitleOn", { recipe: recipeName })
        : t("subtitleOnNoRecipe")
      : t("subtitleOff");

  const attemptWhen = status?.last_attempt_at
    ? new Date(status.last_attempt_at).toLocaleString(locale, {
        day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
      })
    : null;

  return (
    <Strip>
      <span
        className="shrink-0 text-[10px] font-medium uppercase"
        style={{ color: C.textDim, letterSpacing: "0.08em" }}
      >
        {t("title")}
      </span>

      {/* Gleiche Schalter-Optik wie der Runtime-Autostart im Detail-Panel —
          ein Chip, der zufällig bedienbar ist. min-h für WCAG 2.5.8. */}
      <button
        type="button"
        role="switch"
        aria-checked={unknown ? false : enabled}
        aria-label={t("ariaLabel")}
        data-testid="host-autostart-switch"
        data-state={unknown ? "unknown" : enabled ? "on" : "off"}
        title={!isAdmin ? t("adminOnly") : undefined}
        disabled={unknown || busy || !isAdmin}
        onClick={() => mutation.mutate(!enabled)}
        className="shrink-0 inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 min-h-11 sm:min-h-6 text-[10px] font-medium leading-none transition-opacity"
        style={{
          border: `1px solid ${!unknown && enabled ? `${STATUS.online}40` : C.borderActive}`,
          color: unknown ? C.textDim : enabled ? STATUS_TEXT.online : C.textMuted,
          background: "transparent",
          cursor: unknown || busy || !isAdmin ? "not-allowed" : "pointer",
          opacity: busy ? 0.6 : 1,
        }}
      >
        {busy ? <Loader2 size={10} className="animate-spin" aria-hidden /> : <Power size={10} aria-hidden />}
        {loading ? t("loading") : unknown ? t("statusUnknown") : enabled ? t("statusOn") : t("statusOff")}
      </button>

      {!loading && (
        <span className="text-[11px] min-w-0" style={{ color: C.textMuted }} data-testid="host-autostart-subtitle">
          {subtitle}
        </span>
      )}

      {/* Was beim letzten Mal wirklich passiert ist — ein Satz vom Backend,
          kein Statuscode. */}
      {status?.last_result && (
        <span
          className="text-[10px] w-full whitespace-normal"
          style={{ color: C.textDim }}
          data-testid="host-autostart-last-attempt"
        >
          {attemptWhen
            ? t("lastAttemptAt", { when: attemptWhen, result: status.last_result })
            : t("lastAttempt", { result: status.last_result })}
        </span>
      )}

      {error && (
        <span
          className="text-[11px] w-full whitespace-normal"
          style={{ color: STATUS_TEXT.error }}
          role="alert"
          data-testid="host-autostart-error"
        >
          {error}
        </span>
      )}
    </Strip>
  );
}

/** Der Streifen selbst — gleiche Kante und Grundfarbe wie der Geräte-Streifen. */
function Strip({ children }: { children: React.ReactNode }) {
  return (
    <div
      data-testid="host-autostart"
      className="flex flex-wrap items-center gap-x-2.5 gap-y-1 px-4 py-2.5"
      style={{ borderTop: `1px solid ${C.borderSubtle}`, background: C.bgBase }}
    >
      {children}
    </div>
  );
}

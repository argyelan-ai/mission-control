"use client";

/**
 * BoxCockpit — die Schublade je Box (Spec §4). Ersetzt das alte
 * `RuntimeDetailPanel` als Zahnrad-Ziel auf der Bühne (`ActionBar`/`FreeBox`/
 * `AsleepBox`); `RuntimeDetailPanel` bleibt nur noch für Cloud/Unassigned.
 *
 * Desktop: Drawer von rechts, 420px (SlideOverPanel, `hideHeader` — der
 * Kopf hier ist eigen: Boxname (Clash 20px) + Mono-Fakten statt des
 * generischen Panel-Titels). Handy: Bottom-Sheet volle Breite, Grip oben —
 * beides kommt fertig aus SlideOverPanel (gleiche Mechanik wie
 * RuntimeDetailPanel/RepoDetailPanel/LoopDetailPanel), Esc/Backdrop
 * eingeschlossen. Fokus-Falle: beim Öffnen wandert der Fokus auf den
 * Schliessen-Knopf, Tab/Shift-Tab bleiben innerhalb des Inhalts (spec §4).
 *
 * Fünf Gruppen in fester Anatomie [Label 92px | Inhalt] (`.cockpit-grp` in
 * globals.css), Hairlines dazwischen, keine Boxen in Boxen — genau wie das
 * Mockup (`.dw2 .grp`). Bei Duo zusätzlich ein Umschalter im Kopf
 * ("DGX Spark | GX10"), um zwischen den Mitgliedern derselben Bühne zu
 * wechseln, ohne die Schublade zu schliessen.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { RefreshCw, RotateCcw, X } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import type { Device, Host, Runtime } from "@/lib/types";
import { SlideOverPanel } from "@/components/shared/SlideOverPanel";
import { humanApiError } from "@/components/shared/HostRecipeSwitcher";
import { useAppStore } from "@/lib/store";
import { parseStopConflict } from "../ActionBar";
import { TelemetryChart } from "./TelemetryChart";
import { ModeList } from "./ModeList";
import { AutostartGroup } from "./AutostartGroup";
import { ConnectionGroup } from "./ConnectionGroup";
import { RecipeGroup } from "./RecipeGroup";

export interface BoxCockpitMember {
  host: Host;
  role: "head" | "worker" | null;
  device?: Device;
  /**
   * Die Slot-Runtime dieser Box (ADR-078, `is_slot=true`) — Team-Lead-Fund
   * 06.09.2026: die Connection-URL muss IMMER diese Adresse zeigen, nicht die
   * der laufenden Engine (`runtime` unten kann an der LAN-Adresse hängen,
   * während die Slot-Zeile die stabile Adresse ist, an der Agenten hängen).
   */
  slot?: Runtime | null;
}

function GroupRow({ label, children, first, testId }: { label: string; children: React.ReactNode; first?: boolean; testId: string }) {
  return (
    <div
      className="cockpit-grp px-4 py-4"
      style={{ borderTop: first ? "none" : `1px solid ${C.borderSubtle}` }}
      data-testid={testId}
    >
      <span className="font-mono uppercase pt-0.5" style={{ fontSize: "10px", letterSpacing: "0.14em", color: C.textMuted }}>
        {label}
      </span>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

function focusables(root: HTMLElement | null): HTMLElement[] {
  if (!root) return [];
  return Array.from(
    root.querySelectorAll<HTMLElement>(
      'button:not([disabled]), a[href], input:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )
  ).filter((el) => el.offsetParent !== null);
}

export function BoxCockpit({
  open,
  onClose,
  members,
  activeHostId,
  onSwitchActive,
  runtime,
}: {
  open: boolean;
  onClose: () => void;
  /** Alle Boxen dieser Bühne (1 = solo/free/asleep, 2 = duo). */
  members: BoxCockpitMember[];
  activeHostId: string | null;
  onSwitchActive: (hostId: string) => void;
  /** Die Runtime dieser Bühne — null für eine freie Box ohne Slot. */
  runtime: Runtime | null;
}) {
  const t = useTranslations("runtimes.cockpit");
  const tHosts = useTranslations("runtimes.hosts");
  const currentUser = useAppStore((s) => s.currentUser);
  const isAdmin = currentUser?.role === "admin";
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<ReturnType<typeof parseStopConflict>>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const closeBtnRef = useRef<HTMLButtonElement>(null);

  const active = useMemo(
    () => members.find((m) => m.host.id === activeHostId) ?? members[0] ?? null,
    [members, activeHostId]
  );
  const siblings = members.filter((m) => m.host.id !== active?.host.id);

  // Fokus-Falle (Spec §4): beim Öffnen auf den Schliessen-Knopf, Tab/Shift-Tab
  // bleiben innerhalb des Inhalts.
  useEffect(() => {
    if (!open) return;
    closeBtnRef.current?.focus();
  }, [open]);

  useEffect(() => {
    setError(null);
    setConflict(null);
  }, [active?.host.id]);

  const onTrapKeyDown = (e: React.KeyboardEvent) => {
    if (e.key !== "Tab") return;
    const els = focusables(contentRef.current);
    if (els.length === 0) return;
    const first = els[0];
    const last = els[els.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["runtimes"] });
    queryClient.invalidateQueries({ queryKey: ["runtimes", "live-status"] });
    queryClient.invalidateQueries({ queryKey: ["hosts"] });
  };

  const probeMutation = useMutation({
    mutationFn: () => api.runtimes.probeModel(runtime!.id),
    onSuccess: invalidate,
    onError: (err: Error) => setError(t("reprobeFailed", { message: humanApiError(err) })),
  });
  const restartMutation = useMutation({
    mutationFn: () => api.runtimes.restart(runtime!.id),
    onSuccess: invalidate,
    onError: (err: Error) => setError(t("restartFailed", { message: humanApiError(err) })),
  });
  const stopMutation = useMutation({
    mutationFn: (force: boolean) => api.runtimes.stop(runtime!.id, { force }),
    onMutate: () => setError(null),
    onSuccess: (result) => {
      setConflict(null);
      invalidate();
      setError(result.autostart_disabled ? t("stoppedAutostartOff") : null);
    },
    onError: (err: Error) => {
      const parsed = parseStopConflict(err);
      if (parsed) {
        setConflict(parsed);
        return;
      }
      setError(t("stopFailed", { message: humanApiError(err) }));
    },
  });

  if (!active) return null;

  // Team-Lead-Fund 06.09.2026: die Connection-URL zeigt die Slot-Runtime
  // dieser Box (ADR-078), nicht `runtime.endpoint` (die laufende Engine kann
  // an der LAN-Adresse hängen statt der stabilen Adresse, an der Agenten
  // wirklich hängen). Fällt auf `runtime.endpoint` zurück, wenn diese Box
  // (noch) keine eigene Slot-Zeile hat.
  const connectionEndpoint = active.slot?.endpoint ?? runtime?.endpoint ?? null;

  const facts = [
    active.role ? (active.role === "head" ? tHosts("roleHead") : tHosts("roleWorker")) : null,
    active.host.ssh_host,
    active.host.fabric_ip && active.host.fabric_ip !== active.host.ssh_host ? t("fabricPrefix", { ip: active.host.fabric_ip }) : null,
  ].filter(Boolean);

  return (
    <SlideOverPanel open={open} onClose={onClose} title={active.host.display_name} desktopWidth="420px" hideHeader>
      <div ref={contentRef} onKeyDown={onTrapKeyDown} style={{ containerType: "inline-size" }} data-testid="box-cockpit">
        <div className="grid px-4 pt-3.5 pb-3.5" style={{ gridTemplateColumns: "1fr auto", gap: "2px 12px", borderBottom: `1px solid ${C.borderSubtle}` }}>
          <span className="display font-semibold" style={{ fontSize: "20px", color: C.textPrimary, gridColumn: 1, gridRow: 1 }}>
            {active.host.display_name}
          </span>
          <button
            ref={closeBtnRef}
            type="button"
            onClick={onClose}
            aria-label={t("closeAria")}
            data-testid="cockpit-close"
            className="flex items-center justify-center rounded-md cursor-pointer shrink-0 min-h-touch min-w-touch"
            style={{ border: `1px solid ${C.borderActive}`, gridColumn: 2, gridRow: "1 / 3" }}
          >
            <X size={14} style={{ color: C.textSecondary }} />
          </button>
          {facts.length > 0 && (
            <span className="font-mono truncate" style={{ fontSize: "11px", color: C.textMuted, gridColumn: 1 }}>
              {facts.join(" · ")}
            </span>
          )}
          {siblings.length > 0 && (
            <div className="flex gap-1 mt-2" style={{ gridColumn: "1 / -1" }} role="tablist" aria-label={t("boxSwitcherAria")}>
              {members.map((m) => (
                <button
                  key={m.host.id}
                  type="button"
                  role="tab"
                  aria-selected={m.host.id === active.host.id}
                  data-testid={`cockpit-box-switch-${m.host.id}`}
                  onClick={() => onSwitchActive(m.host.id)}
                  className="text-[11px] font-mono px-2.5 py-1.5 rounded-sm cursor-pointer min-h-11 sm:min-h-0"
                  style={{
                    background: m.host.id === active.host.id ? C.accentSubtle : "transparent",
                    color: m.host.id === active.host.id ? C.textPrimary : C.textMuted,
                    border: `1px solid ${m.host.id === active.host.id ? C.borderAccent : C.borderActive}`,
                  }}
                >
                  {m.host.display_name}
                </button>
              ))}
            </div>
          )}
        </div>

        <GroupRow label={t("groupTelemetry")} testId="cockpit-group-telemetry" first>
          <TelemetryChart hostId={active.host.id} />
        </GroupRow>

        <GroupRow label={t("groupMode")} testId="cockpit-group-mode">
          <ModeList device={active.device} canControl={isAdmin} />
        </GroupRow>

        <GroupRow label={t("groupAutostart")} testId="cockpit-group-autostart">
          <AutostartGroup hostId={active.host.id} isAdmin={isAdmin} />
        </GroupRow>

        {connectionEndpoint ? (
          <>
            <GroupRow label={t("groupConnection")} testId="cockpit-group-connection">
              <ConnectionGroup
                // Team-Lead-Fund 06.09.2026: die gebundenen Agenten hängen an
                // der Slot-Runtime dieser Box (ADR-078), nicht am laufenden
                // Rezept — Slot zuerst, `runtime` nur als Fallback für Boxen
                // ohne eigene Slot-Zeile.
                runtimeSlug={(active.slot?.slug ?? active.slot?.id ?? runtime?.slug ?? runtime?.id) as string}
                endpoint={connectionEndpoint}
              />
            </GroupRow>
            {runtime && (
              <GroupRow label={t("groupRecipe")} testId="cockpit-group-recipe">
                <RecipeGroup hostId={active.host.id} runtime={runtime} workerHost={siblings[0]?.host ?? null} />
              </GroupRow>
            )}
          </>
        ) : (
          <GroupRow label={t("groupConnection")} testId="cockpit-group-connection">
            <span className="text-[11px]" style={{ color: C.textMuted }}>{t("noModelHint")}</span>
          </GroupRow>
        )}

        {error && (
          <div className="px-4 pt-3 text-[11px]" style={{ color: C.textSecondary }} data-testid="cockpit-message">
            {error}
          </div>
        )}

        {conflict && (
          <div className="flex items-center gap-3 flex-wrap px-4 pt-3 text-xs" style={{ color: C.textSecondary }} data-testid="cockpit-stop-conflict">
            <span>{t("stopConflict", { agents: conflict.agents.map((a) => a.name).join(", ") })}</span>
            <div className="flex items-center gap-2 ml-auto">
              <button
                type="button"
                onClick={() => setConflict(null)}
                className="px-3 py-2 rounded-md cursor-pointer"
                style={{ border: `1px solid ${C.borderActive}`, color: C.textSecondary }}
              >
                {t("cancel")}
              </button>
              <button
                type="button"
                onClick={() => stopMutation.mutate(true)}
                disabled={stopMutation.isPending}
                data-testid="cockpit-stop-anyway"
                className="px-3 py-2 rounded-md cursor-pointer disabled:opacity-50"
                style={{ border: `1px solid ${C.error}`, color: C.error }}
              >
                {t("stopAnyway")}
              </button>
            </div>
          </div>
        )}

        <div className="cockpit-ft px-4 py-3 mt-3" style={{ background: C.bgDeep }}>
          <button
            type="button"
            onClick={() => probeMutation.mutate()}
            disabled={!runtime || probeMutation.isPending}
            data-testid="cockpit-reprobe"
            className="inline-flex items-center justify-center gap-1.5 text-xs px-3 rounded-md cursor-pointer disabled:opacity-40 min-h-touch sm:min-h-9"
            style={{ border: `1px solid ${C.borderActive}`, color: C.textSecondary }}
          >
            <RefreshCw size={12} />
            {t("reprobe")}
          </button>
          <button
            type="button"
            onClick={() => restartMutation.mutate()}
            disabled={!runtime || restartMutation.isPending}
            data-testid="cockpit-restart"
            title={
              (runtime?.member_hosts ?? []).length > 0
                ? t("restartMultiNodeHint")
                : t("restartHint")
            }
            className="inline-flex items-center justify-center gap-1.5 text-xs px-3 rounded-md cursor-pointer disabled:opacity-40 min-h-touch sm:min-h-9"
            style={{ border: `1px solid ${C.borderActive}`, color: C.textSecondary }}
          >
            <RotateCcw size={12} />
            {t("restart")}
          </button>
          <span className="cockpit-ft-spacer" />
          <button
            type="button"
            onClick={() => stopMutation.mutate(false)}
            disabled={!runtime || stopMutation.isPending}
            data-testid="cockpit-stop"
            className="cockpit-ft-stop text-xs px-3 rounded-md cursor-pointer disabled:opacity-40 min-h-touch sm:min-h-9"
            style={{ color: C.error, border: `1px solid ${C.borderSubtle}` }}
          >
            {t("stopAutostartOff")}
          </button>
        </div>
      </div>
    </SlideOverPanel>
  );
}

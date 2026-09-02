"use client";

/**
 * HostRecipeSwitcher — der EINE Rezept-Umschalter je Box.
 *
 * Vertrag 02.09.2026 (docs/plans/2026-09-02-rezept-umschalter-vertrag.md):
 * Gerätekachel (SlotStage) und Detail-Panel zeigen dieselbe Liste aus
 * derselben Quelle — GET /hosts/{host_id}/recipes. Darum lebt der ganze
 * Umschalter hier in einer Komponente und nicht zweimal.
 *
 * Was das Backend sagt, wird gezeigt, nicht nachgerechnet:
 *   - zwei Gruppen nach `topology.nodes` („Nur diese Box" · „Beide Boxen")
 *   - Reihenfolge laufend → startbar → grau
 *   - grau = `startable:false`, mit `reason` als Satz IN der Zeile (auf dem
 *     Handy gibt es keinen Hover, ein Tooltip wäre dort unsichtbar)
 *   - „läuft" nur bei `running:true`; nach dem Klick „startet …", bis die
 *     Liste selbst umschlägt — kein vorgetäuschter Zustand
 *   - Fehler (409/422) bleiben als Satz stehen, bis der Operator sie
 *     wegklickt — ein Toast wäre weg, bevor man ihn gelesen hat
 *
 * Zwei Klicks bis zum Start (wählen → bestätigen): ein Start wirft das
 * laufende Modell der Box raus, das darf nie ein einzelner Fehlklick sein.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { api } from "@/lib/api";
import { C, STATUS, STATUS_TEXT } from "@/lib/colors";
import type { HostRecipe } from "@/lib/types";

// Ein Schlüssel für alle Leser — SlotStage, Detail-Panel und der Deploy im
// Modell-Browser invalidieren denselben Cache-Eintrag.
export const hostRecipesKey = (hostId: string) => ["hosts", hostId, "recipes"] as const;

/**
 * Nach einem Start pollen wir die Liste, bis `running` umschlägt. Läuft ein
 * Rezept auch nach dieser Frist nicht, ist die Anzeige „startet …" nicht mehr
 * ehrlich (der Start ist wahrscheinlich gescheitert) — dann zeigt die Liste
 * wieder nur, was das Backend meldet.
 */
const STARTING_TIMEOUT_MS = 15 * 60 * 1000;
const POLL_WHILE_STARTING_MS = 5_000;

export function useHostRecipes(hostId: string | null | undefined, opts?: { poll?: boolean }) {
  return useQuery({
    queryKey: hostRecipesKey(hostId ?? ""),
    queryFn: () => api.hosts.recipes(hostId as string),
    enabled: !!hostId,
    staleTime: 30_000,
    refetchInterval: opts?.poll ? POLL_WHILE_STARTING_MS : false,
    refetchOnWindowFocus: false,
  });
}

// Präsentationsreihenfolge innerhalb einer Gruppe. Das Backend liefert die
// Liste schon so sortiert; wir sortieren stabil nach, damit die Reihenfolge
// auch nach dem Aufteilen in zwei Gruppen stimmt.
function rank(r: HostRecipe): number {
  if (r.running) return 0;
  if (r.startable) return 1;
  return 2;
}

export function groupHostRecipes(recipes: HostRecipe[]): { solo: HostRecipe[]; duo: HostRecipe[] } {
  const sorted = [...recipes].sort((a, b) => rank(a) - rank(b));
  return {
    solo: sorted.filter((r) => r.topology.nodes === 1),
    duo: sorted.filter((r) => r.topology.nodes !== 1),
  };
}

/**
 * request() wirft `API 409: {"detail":"…"}`. Der Operator soll den Satz aus
 * `detail` lesen, nicht das JSON drumherum — genau dafür schickt das Backend
 * Gründe als Sätze (Vertrag: „Grau-Gründe sind Sätze, keine Codes").
 */
export function humanApiError(err: Error): string {
  const m = /^API \d+: ([\s\S]*)$/.exec(err.message);
  if (!m) return err.message;
  try {
    const parsed = JSON.parse(m[1]) as { detail?: unknown };
    if (typeof parsed.detail === "string" && parsed.detail.trim()) return parsed.detail;
  } catch {
    // kein JSON — dann ist der Text selbst die Meldung
  }
  return m[1];
}

const MENU_WIDTH = 380;
const MENU_MARGIN = 8;

export function HostRecipeSwitcher({
  hostId,
  servingName = null,
  compact = false,
  hideWhenEmpty = false,
}: {
  hostId: string;
  /** Name dessen, was die Box gerade fährt — als Fallback-Beschriftung, wenn
   *  kein Rezept `running` meldet (z.B. ein Modell ausserhalb des Katalogs). */
  servingName?: string | null;
  /** Kleinere Auslöser-Schaltfläche, passend zu den Aktionsknöpfen im Panel. */
  compact?: boolean;
  /** Detail-Panel: ohne Rezepte gibt es dort nichts zu schalten — Kachel
   *  dagegen zeigt den Auslöser immer, damit die Zeile nicht verschwindet. */
  hideWhenEmpty?: boolean;
}) {
  const t = useTranslations("runtimes.recipeSwitcher");
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [confirm, setConfirm] = useState<HostRecipe | null>(null);
  const [starting, setStarting] = useState<{ slug: string; name: string; since: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pos, setPos] = useState<{ top?: number; bottom?: number; left: number; width: number; maxHeight: number } | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  const recipesQuery = useHostRecipes(hostId, { poll: starting != null });
  const recipes = useMemo(() => recipesQuery.data ?? [], [recipesQuery.data]);
  const groups = useMemo(() => groupHostRecipes(recipes), [recipes]);
  const runningRecipe = recipes.find((r) => r.running) ?? null;

  // „startet …" endet, sobald die Liste das Rezept als laufend meldet — oder
  // nach der Frist, wenn es offensichtlich nicht mehr kommt.
  useEffect(() => {
    if (!starting) return;
    const nowRunning = recipes.some((r) => r.slug === starting.slug && r.running);
    if (nowRunning || Date.now() - starting.since > STARTING_TIMEOUT_MS) setStarting(null);
  }, [recipes, starting]);

  const startMutation = useMutation({
    mutationFn: (recipe: HostRecipe) => api.hosts.startRecipe(hostId, recipe.slug),
    onMutate: (recipe) => {
      setError(null);
      setConfirm(null);
      setStarting({ slug: recipe.slug, name: recipe.display_name, since: Date.now() });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: hostRecipesKey(hostId) });
      queryClient.invalidateQueries({ queryKey: ["runtimes"] });
    },
    onError: (err: Error) => {
      setStarting(null);
      setError(t("startFailed", { message: humanApiError(err) }));
    },
  });

  // Klick ausserhalb schliesst Liste und Bestätigung, Escape ebenso.
  useEffect(() => {
    if (!open && confirm == null) return;
    const onPointerDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (rootRef.current?.contains(target) || menuRef.current?.contains(target)) return;
      setOpen(false);
      setConfirm(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        setConfirm(null);
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, confirm]);

  // Die Liste liegt in einem Portal (die Kachel schneidet Überlauf ab) und
  // wird am Rand des Fensters eingeklemmt — auf 390 px Handybreite darf sie
  // weder rechts rausragen noch unten abgeschnitten werden.
  useEffect(() => {
    if (!open) return;
    const update = () => {
      const el = triggerRef.current;
      if (!el) return;
      const r = el.getBoundingClientRect();
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const width = Math.min(MENU_WIDTH, vw - MENU_MARGIN * 2);
      const left = Math.max(MENU_MARGIN, Math.min(r.left, vw - width - MENU_MARGIN));
      const spaceBelow = vh - r.bottom - MENU_MARGIN;
      const spaceAbove = r.top - MENU_MARGIN;
      const dropUp = spaceBelow < 240 && spaceAbove > spaceBelow;
      const maxHeight = Math.max(160, Math.min(320, (dropUp ? spaceAbove : spaceBelow) - 4));
      setPos(
        dropUp
          ? { bottom: vh - r.top + 4, left, width, maxHeight }
          : { top: r.bottom + 4, left, width, maxHeight },
      );
    };
    update();
    window.addEventListener("scroll", update, true);
    window.addEventListener("resize", update);
    return () => {
      window.removeEventListener("scroll", update, true);
      window.removeEventListener("resize", update);
    };
  }, [open]);

  if (hideWhenEmpty && recipesQuery.isSuccess && recipes.length === 0) return null;

  const triggerLabel = runningRecipe?.display_name ?? servingName ?? t("selectRecipe");
  const isPending = startMutation.isPending || starting != null;

  const select = (recipe: HostRecipe) => {
    setOpen(false);
    setConfirm(recipe);
  };

  return (
    // display:contents — Auslöser, Bestätigung und Fehlerzeile sind direkte
    // Kinder der umgebenden Zeile (flex-wrap). So bleibt „+ Modell" neben dem
    // Auslöser, und die Fehlerzeile bricht als eigene Zeile unter die ganze
    // Zeile statt nur unter den Auslöser.
    <div ref={rootRef} className="contents" data-testid="host-recipe-switcher">
      {confirm == null && !isPending && (
        <button
          ref={triggerRef}
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-haspopup="listbox"
          aria-expanded={open}
          data-testid="recipe-dropdown-trigger"
          className={`flex items-center gap-2 rounded-md cursor-pointer max-w-full ${compact ? "h-7 px-2.5 text-[11px]" : "px-3 py-2 text-xs"}`}
          style={{
            background: C.bgSurface,
            border: `1px solid ${open ? C.borderAccent : C.border}`,
            color: C.textPrimary,
          }}
        >
          {runningRecipe && (
            <span aria-hidden className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: STATUS.online }} />
          )}
          <span className="font-mono truncate max-w-[260px]">{triggerLabel}</span>
          <span aria-hidden style={{ color: C.textDim, fontSize: "9px" }}>▾</span>
        </button>
      )}

      {/* Bestätigung ersetzt den Auslöser, statt ein Modal zu öffnen — die
          Entscheidung bleibt dort, wo sie angestossen wurde. */}
      {confirm != null && !isPending && (
        <div
          className="flex items-center gap-2 rounded-md px-3 py-2 text-xs flex-wrap"
          style={{ background: C.bgSurface, border: `1px solid ${C.borderAccent}` }}
          data-testid="recipe-confirm"
        >
          <span className="font-mono truncate max-w-[260px]" style={{ color: C.textPrimary }}>{confirm.display_name}</span>
          <button
            type="button"
            onClick={() => startMutation.mutate(confirm)}
            className="rounded-sm px-2 py-1 text-[10px] font-semibold cursor-pointer"
            style={{ background: C.accent, color: C.bgDeep }}
            data-testid="recipe-confirm-start"
          >
            {t("confirmStart")}
          </button>
          <button
            type="button"
            onClick={() => setConfirm(null)}
            className="rounded-sm px-2 py-1 text-[10px] cursor-pointer"
            style={{ border: `1px solid ${C.borderSubtle}`, color: C.textMuted }}
          >
            {t("cancel")}
          </button>
          {servingName && (
            <span className="text-[10px]" style={{ color: C.textMuted }}>{t("evictHint", { name: servingName })}</span>
          )}
        </div>
      )}

      {/* Ehrlicher Zwischenzustand: der Befehl ist raus, laufen tut noch
          nichts. Bleibt stehen, bis die Liste `running` meldet. */}
      {isPending && (
        <div
          className={`flex items-center gap-2 rounded-md text-xs ${compact ? "h-7 px-2.5" : "px-3 py-2"}`}
          style={{ background: C.bgSurface, border: `1px solid ${C.border}`, color: STATUS_TEXT.warning }}
          data-testid="recipe-starting"
          role="status"
        >
          <span aria-hidden className="w-1.5 h-1.5 rounded-full shrink-0 animate-pulse" style={{ background: STATUS.warning }} />
          <span className="font-mono truncate max-w-[260px]">{t("starting", { name: starting?.name ?? "" })}</span>
        </div>
      )}

      {recipesQuery.isError && (
        <span className="text-xs w-full order-last" style={{ color: STATUS_TEXT.error }} data-testid="recipe-load-error">
          {t("loadFailed", { message: recipesQuery.error.message })}
        </span>
      )}

      {error && (
        <span
          // order-last: die Fehlerzeile bricht als LETZTE Zeile unter alle
          // Nachbarn der umgebenden Zeile (z.B. „+ Modell"), nie dazwischen.
          className="flex items-start gap-2 text-xs w-full order-last"
          style={{ color: STATUS_TEXT.error }}
          role="alert"
          data-testid="recipe-start-error"
        >
          <span className="min-w-0">{error}</span>
          <button
            type="button"
            onClick={() => setError(null)}
            aria-label={t("dismiss")}
            className="shrink-0 cursor-pointer px-1 leading-none"
            style={{ color: C.textMuted }}
          >
            ×
          </button>
        </span>
      )}

      {open && pos != null && typeof document !== "undefined" &&
        createPortal(
          <div
            ref={menuRef}
            role="listbox"
            aria-label={t("listLabel")}
            data-testid="recipe-dropdown-list"
            className="fixed z-50 rounded-md overflow-y-auto"
            style={{
              top: pos.top,
              bottom: pos.bottom,
              left: pos.left,
              width: pos.width,
              maxHeight: pos.maxHeight,
              background: C.bgElevated,
              border: `1px solid ${C.borderActive}`,
              boxShadow: "0 8px 24px rgba(0,0,0,0.5)",
            }}
          >
            {recipesQuery.isLoading && (
              <div className="px-3 py-2.5 text-xs" style={{ color: C.textMuted }}>{t("loading")}</div>
            )}
            {recipesQuery.isSuccess && recipes.length === 0 && (
              <div className="px-3 py-2.5 text-xs" style={{ color: C.textMuted }} data-testid="recipe-empty">
                {t("empty")}
              </div>
            )}
            {recipesQuery.isError && (
              <div className="px-3 py-2.5 text-xs" style={{ color: STATUS_TEXT.error }}>
                {t("loadFailed", { message: recipesQuery.error.message })}
              </div>
            )}
            {groups.solo.length > 0 && (
              <RecipeGroup label={t("groupSolo")} recipes={groups.solo} testId="recipe-group-solo" onSelect={select} t={t} />
            )}
            {groups.duo.length > 0 && (
              <RecipeGroup label={t("groupDuo")} recipes={groups.duo} testId="recipe-group-duo" onSelect={select} t={t} />
            )}
          </div>,
          document.body,
        )}
    </div>
  );
}

function RecipeGroup({
  label,
  recipes,
  testId,
  onSelect,
  t,
}: {
  label: string;
  recipes: HostRecipe[];
  testId: string;
  onSelect: (r: HostRecipe) => void;
  t: ReturnType<typeof useTranslations>;
}) {
  return (
    <div role="group" aria-label={label} data-testid={testId}>
      <div
        className="flex items-baseline gap-2 px-3 pt-2.5 pb-1 text-[9px] font-medium uppercase"
        style={{ color: C.textMuted, letterSpacing: "0.1em" }}
      >
        <span>{label}</span>
        <span className="tabular-nums" style={{ color: C.textDim }}>{recipes.length}</span>
      </div>
      {recipes.map((r) => {
        const disabled = r.running || !r.startable;
        const busy = !r.running && r.busy_hosts.length > 0;
        return (
          <button
            key={r.slug}
            type="button"
            role="option"
            aria-selected={r.running}
            disabled={disabled}
            data-testid={`recipe-option-${r.slug}`}
            data-running={r.running ? "true" : "false"}
            data-startable={r.startable ? "true" : "false"}
            onClick={() => onSelect(r)}
            className="flex flex-col gap-0.5 w-full px-3 py-2 text-left text-xs cursor-pointer disabled:cursor-not-allowed transition-colors enabled:hover:bg-[var(--color-bg-hover)]"
            style={{
              color: r.running ? C.accent : r.startable ? C.textPrimary : C.textDim,
              borderBottom: `1px solid ${C.borderSubtle}`,
            }}
          >
            <div className="flex items-center gap-2 w-full min-w-0">
              {r.running && (
                <span aria-hidden className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: STATUS.online }} />
              )}
              <span className="font-mono truncate">{r.display_name}</span>
              <span className="ml-auto shrink-0 text-[10px] tabular-nums" style={{ color: C.textMuted }}>
                {r.port != null ? `:${r.port}` : ""}
              </span>
              {r.running && (
                <span className="shrink-0 text-[9px] uppercase" style={{ color: STATUS.online, letterSpacing: "0.08em" }}>
                  {t("running")}
                </span>
              )}
            </div>
            {busy && (
              <span className="text-[10px]" style={{ color: C.textMuted }} data-testid={`recipe-busy-${r.slug}`}>
                {t("busyOn", { hosts: r.busy_hosts.join(", ") })}
              </span>
            )}
            {/* Der Grund steht in der Zeile, nicht im Tooltip — Handy. */}
            {!r.startable && r.reason && (
              <span className="text-[10px] whitespace-normal" style={{ color: C.textMuted }} data-testid={`recipe-reason-${r.slug}`}>
                {r.reason}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

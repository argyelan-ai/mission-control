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
import { useLocale, useTranslations } from "next-intl";
import { ChevronDown } from "lucide-react";
import { api } from "@/lib/api";
import { pausedRuntimeDetail } from "@/lib/recipeStart";
import { C, STATUS, STATUS_TEXT, alpha } from "@/lib/colors";
import { useHeadConflictText } from "@/components/heads/HeadOccupancy";
import type { HostRecipe } from "@/lib/types";
import { benchEnabled, scheduleBenchAutoTest } from "@/lib/benchLinks";

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

/**
 * Duo-Start (P3): das Rezept läuft über zwei Boxen. Welche die zweite ist,
 * entscheidet der Operator in der Bestätigungszeile — nicht das Backend
 * heimlich. Drei Fälle, alle aus echten Feldern:
 *   - mehrere Kandidaten → Auswahlfeld
 *   - genau einer        → sein Name als Text (nichts zu wählen)
 *   - keiner             → Start gesperrt, mit Grund als Satz
 * Fehlt dem Rezept die Umgebungs-Zuordnung (`env_ready:false`), ist der Start
 * ebenfalls gesperrt — MC könnte die .env auf dem Kopf nicht schreiben.
 */
/**
 * Vorflug-Hinweise (P4): das Backend prüft RAM und Platte aller Ziel-Boxen
 * aus der Telemetrie und schickt Sätze. Sie sperren nichts — ein harter
 * Verstoss kommt als `startable:false` mit `reason`. Ältere Backends schicken
 * das Feld nicht; dann gibt es eben nichts zu zeigen.
 */
export function capacityWarnings(recipe: HostRecipe): string[] {
  return recipe.capacity?.warnings ?? [];
}

export function duoStartBlocker(recipe: HostRecipe): "env" | "no-worker" | null {
  if (recipe.topology.nodes === 1) return null;
  if (recipe.env_ready === false) return "env";
  if (recipe.candidate_workers.length === 0) return "no-worker";
  return null;
}

export function HostRecipeSwitcher({
  hostId,
  hostName = null,
  servingName = null,
  compact = false,
  hideWhenEmpty = false,
  label = null,
  primary = false,
  secondary = false,
  blockedBy = null,
  onBlocked,
  onActiveChange,
}: {
  hostId: string;
  /** Name dieser Box — die Erfolgsmeldung eines Duo-Starts nennt beide Boxen. */
  hostName?: string | null;
  /** Name dessen, was die Box gerade fährt — als Fallback-Beschriftung, wenn
   *  kein Rezept `running` meldet (z.B. ein Modell ausserhalb des Katalogs). */
  servingName?: string | null;
  /** Kleinere Auslöser-Schaltfläche, passend zu den Aktionsknöpfen im Panel. */
  compact?: boolean;
  /** Detail-Panel: ohne Rezepte gibt es dort nichts zu schalten — Kachel
   *  dagegen zeigt den Auslöser immer, damit die Zeile nicht verschwindet. */
  hideWhenEmpty?: boolean;
  /** Runtimes-Bühne v2 (Spec §2 Zone 4): "Switch model"/"Start model"/"Other
   *  model" statt des laufenden Rezeptnamens — das Rezept steht dort schon
   *  im Kartentitel, der Auslöser ist eine Handlung, kein zweiter Statuswert. */
  label?: string | null;
  /** Runtimes-Bühne v2: Akzentfläche + dunkler Text statt der ruhigen
   *  Registerzeile — für die primäre Aktion der Karte (Switch/Start model). */
  primary?: boolean;
  /** Bench W1 P1: on the card that leads with "Testen" the switch is the
   *  second action — a bordered button in the sans voice, 44px tall. */
  secondary?: boolean;
  /** Head launcher §6.7: a head works on this box — switching would cut it
   *  off. The trigger stays clickable, but a click opens nothing: it calls
   *  `onBlocked` so the caller can show the reason at that moment. The
   *  backend guard (409 head_on_box) stays the real protection. */
  blockedBy?: string | null;
  onBlocked?: () => void;
  /** Bench W1 (review M4): the list or the confirmation is open — the card
   *  then demotes its own primary ("Testen"), so the dialog's "Starten" is
   *  the screen's one accent button (K11). */
  onActiveChange?: (active: boolean) => void;
}) {
  const t = useTranslations("runtimes.recipeSwitcher");
  const tBench = useTranslations("benchHome.autoTest");
  const locale = useLocale();
  const headConflictText = useHeadConflictText();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [confirm, setConfirm] = useState<HostRecipe | null>(null);
  const active = open || confirm != null;
  useEffect(() => {
    onActiveChange?.(active);
  }, [active, onActiveChange]);
  // Gewählte Worker-Box für das Rezept in der Bestätigungszeile (nur Duo).
  const [workerHostId, setWorkerHostId] = useState<string | null>(null);
  const [starting, setStarting] = useState<{ slug: string; name: string; since: number; worker: string | null } | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Bench W1 P1b: "Danach automatisch testen" — on by default, for THIS
  // switch only (reset whenever a recipe is picked). After the switch the
  // planned test (or why it could not be planned) is a quiet line.
  const [autoTest, setAutoTest] = useState(true);
  const [autoTestNote, setAutoTestNote] = useState<
    { kind: "scheduled"; notBefore: string | null } | { kind: "failed" } | null
  >(null);
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
    // Solo bleibt ein Aufruf ohne Körper — nur der Duo-Start schickt die
    // gewählte Worker-Box mit ({"worker_host_id"}, Vertrag P3).
    mutationFn: ({ recipe, worker }: { recipe: HostRecipe; worker: string | null; autoTest: boolean }) =>
      worker
        ? api.hosts.startRecipe(hostId, recipe.slug, { worker_host_id: worker })
        : api.hosts.startRecipe(hostId, recipe.slug),
    onMutate: ({ recipe, worker }) => {
      setError(null);
      setConfirm(null);
      setStarting({
        slug: recipe.slug,
        name: recipe.display_name,
        since: Date.now(),
        worker: recipe.candidate_workers.find((w) => w.host_id === worker)?.slug ?? null,
      });
    },
    onSuccess: (result, { recipe, autoTest: wantsTest }) => {
      // Das Backend nennt die Box, die es wirklich genommen hat — die zeigen
      // wir, nicht unsere Vorauswahl (sie könnte abweichen).
      if (result?.worker_slug) {
        setStarting((s) => (s ? { ...s, worker: result.worker_slug as string } : s));
      }
      queryClient.invalidateQueries({ queryKey: hostRecipesKey(hostId) });
      queryClient.invalidateQueries({ queryKey: ["runtimes"] });
      // Ein Duo-Start ändert die Belegung der Flotte (runtime_hosts) und den
      // gemerkten Autostart-Slug am Kopf — beides hängt an ["hosts"].
      queryClient.invalidateQueries({ queryKey: ["hosts"] });
      // Only after a successful switch, and never able to undo it: a failed
      // schedule is a quiet line, not an error (W1.11).
      if (wantsTest && benchEnabled) {
        scheduleBenchAutoTest(hostId, recipe.slug)
          .then((res) => {
            if (res) setAutoTestNote({ kind: "scheduled", notBefore: res.not_before });
          })
          .catch(() => setAutoTestNote({ kind: "failed" }));
      }
    },
    onError: (err: Error) => {
      setStarting(null);
      const paused = pausedRuntimeDetail(err);
      setError(
        paused
          ? t("startPaused", { slug: paused.runtime_slug })
          : (headConflictText(err) ?? t("startFailed", { message: humanApiError(err) })),
      );
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

  const triggerLabel = label ?? runningRecipe?.display_name ?? servingName ?? t("selectRecipe");
  const isPending = startMutation.isPending || starting != null;

  const select = (recipe: HostRecipe) => {
    setOpen(false);
    setConfirm(recipe);
    setAutoTest(true);
    setAutoTestNote(null);
    // Vorauswahl = erster Kandidat (dieselbe Reihenfolge, die das Backend
    // ohne Angabe nehmen würde) — der Operator kann sie ändern.
    setWorkerHostId(recipe.topology.nodes === 1 ? null : recipe.candidate_workers[0]?.host_id ?? null);
  };

  const blocker = confirm ? duoStartBlocker(confirm) : null;
  const startingLabel = starting
    ? starting.worker
      ? hostName
        ? t("startingDuo", { name: starting.name, head: hostName, worker: starting.worker })
        : t("startingDuoNoHead", { name: starting.name, worker: starting.worker })
      : t("starting", { name: starting.name })
    : "";

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
          onClick={() => (blockedBy ? onBlocked?.() : setOpen((v) => !v))}
          aria-haspopup={blockedBy ? undefined : "listbox"}
          aria-expanded={open}
          data-testid="recipe-dropdown-trigger"
          // Primär (Runtimes-Bühne v2, Spec §2 Zone 4 — Review #438 zweite
          // Sichtprüfung 06.09.2026): eigene Grösse UNABHÄNGIG von `compact`
          // — eine Primäraktion braucht ihre volle Höhe (40px), auch wenn
          // sie neben kompakten Aktionsknöpfen steht. `compact` bleibt nur
          // für den ruhigen Registerzeilen-Auslöser (Panel/SlotStage) in
          // Kraft, wenn `primary` nicht gesetzt ist.
          data-variant={primary ? "primary" : secondary ? "secondary" : "register"}
          className={`flex items-center justify-center gap-2 rounded-md cursor-pointer max-w-full disabled:opacity-40 disabled:cursor-not-allowed ${
            primary
              ? "h-10 px-3.5 text-sm font-semibold"
              : secondary
                ? "h-11 px-3 text-sm font-medium hover:bg-[var(--color-bg-hover)]"
                : compact
                  ? "h-7 px-2.5 text-[11px]"
                  : "px-3 py-2 text-xs"
          }`}
          style={{
            background: primary ? C.accent : secondary ? "transparent" : C.bgSurface,
            border: `1px solid ${primary ? C.accent : open ? C.borderAccent : secondary ? C.borderActive : C.border}`,
            color: primary ? C.onAccent : C.textPrimary,
          }}
        >
          {/* Der Laufpunkt gehört zum Rezeptnamen im Auslöser — mit einem
              eigenen Label (Switch/Start/Other model) steht dort keine
              Statusaussage mehr, die er begleiten könnte. */}
          {runningRecipe && !label && (
            <span aria-hidden className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: STATUS.online }} />
          )}
          <span className={`truncate max-w-[260px] ${primary || secondary ? "" : "font-mono"}`}>{triggerLabel}</span>
          {primary || secondary ? (
            <ChevronDown size={16} aria-hidden style={{ color: primary ? C.onAccent : C.textSecondary }} />
          ) : (
            <span aria-hidden style={{ color: C.textDim, fontSize: "9px" }}>▾</span>
          )}
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

          {/* Zweibox-Rezept: welche Box ist die zweite? Steht IN der Zeile,
              direkt vor „Starten" — die Wahl gehört zur Entscheidung, nicht
              in ein eigenes Fenster. */}
          {confirm.topology.nodes !== 1 && blocker == null && (
            <span className="flex items-center gap-1.5" data-testid="recipe-worker-choice">
              <span className="text-[10px] uppercase" style={{ color: C.textMuted, letterSpacing: "0.08em" }}>
                {t("workerLabel")}
              </span>
              {confirm.candidate_workers.length === 1 ? (
                <span className="font-mono text-[11px]" style={{ color: C.textSecondary }} data-testid="recipe-worker-single">
                  {confirm.candidate_workers[0].slug}
                </span>
              ) : (
                <select
                  aria-label={t("workerLabel")}
                  data-testid="recipe-worker-select"
                  value={workerHostId ?? ""}
                  onChange={(e) => setWorkerHostId(e.target.value)}
                  className="rounded-sm px-1.5 py-0.5 font-mono text-[11px] cursor-pointer"
                  style={{ background: C.bgElevated, border: `1px solid ${C.border}`, color: C.textPrimary }}
                >
                  {confirm.candidate_workers.map((w) => (
                    <option key={w.host_id} value={w.host_id}>{w.slug}</option>
                  ))}
                </select>
              )}
            </span>
          )}

          <button
            type="button"
            disabled={blocker != null}
            onClick={() =>
              startMutation.mutate({
                recipe: confirm,
                worker: confirm.topology.nodes === 1 ? null : workerHostId,
                autoTest: benchEnabled && autoTest,
              })
            }
            // The decision button: 44 px, text from the type scale (K11).
            className="inline-flex items-center min-h-11 px-4 rounded-md text-sm font-semibold cursor-pointer disabled:cursor-not-allowed"
            style={{
              background: blocker != null ? C.bgHover : C.accent,
              color: blocker != null ? C.textDim : C.bgDeep,
            }}
            data-testid="recipe-confirm-start"
          >
            {t("confirmStart")}
          </button>
          <button
            type="button"
            onClick={() => setConfirm(null)}
            className="inline-flex items-center min-h-11 px-3 rounded-md text-sm cursor-pointer"
            style={{ color: C.textSecondary }}
          >
            {t("cancel")}
          </button>
          {servingName && blocker == null && (
            <span className="text-[10px]" style={{ color: C.textMuted }}>{t("evictHint", { name: servingName })}</span>
          )}

          {/* Gesperrt: der Grund als ganzer Satz in der Zeile — wer den Knopf
              grau sieht, soll nicht raten müssen, was fehlt. */}
          {blocker != null && (
            <span
              className="text-[10px] w-full whitespace-normal"
              style={{ color: STATUS_TEXT.warning }}
              data-testid="recipe-worker-blocked"
              data-blocker={blocker}
            >
              {blocker === "env"
                ? confirm.reason ?? t("workerEnvMissing")
                : confirm.reason ?? t("workerNone")}
            </span>
          )}

          {/* Bench W1 P1b: "Danach automatisch testen" belongs to this
              decision, so it sits in the confirmation, not in a dialog. */}
          {benchEnabled && blocker == null && (
            <span className="flex items-start gap-3 w-full pt-2" data-testid="recipe-autotest">
              <button
                type="button"
                role="switch"
                aria-checked={autoTest}
                aria-labelledby="recipe-autotest-label"
                aria-describedby="recipe-autotest-sentence"
                onClick={() => setAutoTest((v) => !v)}
                className="shrink-0 flex items-center justify-center w-11 h-11 -my-3 -ml-1 cursor-pointer rounded-full"
              >
                <span
                  aria-hidden
                  className="relative block w-9 h-5 rounded-full transition-colors"
                  style={{
                    background: autoTest ? C.accent : C.bgElevated,
                    border: `1px solid ${autoTest ? C.accent : C.border}`,
                  }}
                >
                  <span
                    className="absolute w-3.5 h-3.5 rounded-full transition-[left]"
                    style={{ top: "2px", left: autoTest ? "calc(100% - 16px)" : "2px", background: autoTest ? C.onAccent : C.textMuted }}
                  />
                </span>
              </button>
              <span className="flex flex-col gap-1 min-w-0 whitespace-normal">
                <span id="recipe-autotest-label" className="text-xs font-medium" style={{ color: C.textPrimary }}>
                  {tBench("label")}
                </span>
                <span id="recipe-autotest-sentence" className="text-xs" style={{ color: C.textMuted }}>
                  {autoTest ? tBench("on") : tBench("off")}
                </span>
              </span>
            </span>
          )}

          {/* Vorflug-Hinweise dort, wo geklickt wird. Sie sperren nichts —
              wer den Start trotzdem will, klickt ihn; er weiss jetzt nur,
              woran er scheitern kann. */}
          {capacityWarnings(confirm).length > 0 && (
            <span
              className="flex flex-col gap-0.5 text-[10px] w-full whitespace-normal"
              style={{ color: STATUS_TEXT.warning }}
              aria-label={t("capacityNotes")}
              data-testid="recipe-confirm-capacity"
            >
              {capacityWarnings(confirm).map((w) => (
                <span key={w}>{w}</span>
              ))}
            </span>
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
          <span className="font-mono truncate max-w-[420px]">{startingLabel}</span>
        </div>
      )}

      {autoTestNote && (
        <span
          className="text-xs w-full order-last whitespace-normal"
          style={{ color: C.textMuted }}
          data-testid="recipe-autotest-note"
          role="status"
        >
          {autoTestNote.kind === "failed"
            ? tBench("failed")
            : autoTestNote.notBefore && !Number.isNaN(new Date(autoTestNote.notBefore).getTime())
              ? tBench("scheduledAt", {
                  time: new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit" }).format(
                    new Date(autoTestNote.notBefore),
                  ),
                })
              : tBench("scheduled")}
        </span>
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
              boxShadow: `0 8px 24px ${alpha(C.shadow, 0.5)}`,
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
            {/* Vorflug (P4): „geht wahrscheinlich, aber knapp" — Hinweis in
                Warnfarbe, damit man ihn nicht mit einem Riegel verwechselt. */}
            {capacityWarnings(r).length > 0 && (
              <span
                className="flex flex-col gap-0.5 text-[10px] whitespace-normal"
                style={{ color: STATUS_TEXT.warning }}
                aria-label={t("capacityNotes")}
                data-testid={`recipe-capacity-${r.slug}`}
              >
                {capacityWarnings(r).map((w) => (
                  <span key={w}>{w}</span>
                ))}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

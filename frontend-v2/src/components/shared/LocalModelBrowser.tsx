"use client";

/**
 * LocalModelBrowser — kuratierte Modelle für EIGENE Hardware auf /runtimes.
 *
 * Schwester-Sektion der ModelCatalogSection: dort steht, was es beim Anbieter
 * gibt, hier steht, was auf der eigenen Box läuft. Derselbe Vertrag gilt in
 * beide Richtungen — ein Eintrag sagt nur „dieses Rezept EXISTIERT". Die
 * laufende Wahrheit bleibt runtime.model_identifier; `running` ist der vom
 * Backend abgeleitete Hinweis, absichtlich konservativ.
 *
 * Der Deploy legt hier KEINEN zweiten Start-Pfad an: er ruft exakt die
 * Mutation, die der SparkRecipeSwitcher schon benutzt
 * (api.runtimes.sparkrun.switchRecipe). Einträge ohne sparkrun-recipe_ref
 * bleiben deshalb bewusst deaktiviert statt einen Weg anzubieten, der scheitert.
 *
 * ssh_process-Einträge (PR 6) haben noch gar keine Runtime-Zeile und liegen
 * beim ersten Mal auch noch nicht auf der Box — die gehen in den
 * SshProcessDeployDialog (installieren, dann anlegen + über den bestehenden
 * Start-Endpoint starten).
 *
 * Styling: ausschliesslich semantische Klassen aus globals.css — wie in der
 * ModelCatalogSection, kein zweites Designsystem, keine colors.ts-Konstanten.
 */

import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import {
  RotateCcw,
  Loader2,
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  Rocket,
  Search,
  Eye,
  EyeOff,
  ChevronRight,
  ExternalLink,
} from "lucide-react";
import { api } from "@/lib/api";
import type {
  LocalRecipe,
  LocalRegistryRefreshResult,
  Runtime,
} from "@/lib/types";
import { useNotificationStore } from "@/lib/store";
import { timeAgo } from "@/lib/utils";
import { SectionOrFragment } from "@/components/shared/Section";
import { CappedList } from "@/components/shared/CappedList";
import { ListRow, MetaChip, MetaText } from "@/components/shared/ListRow";
import { OverflowMenu } from "@/components/shared/OverflowMenu";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { SshProcessDeployDialog } from "@/components/shared/SshProcessDeployDialog";

/**
 * Grober Passt-Check gegen eine Box der Spark-Klasse (GB10, 128 GB Unified
 * Memory, davon realistisch ~121 GB für Gewichte + KV-Cache nutzbar).
 *
 * Bewusst eine Konstante und bewusst nur eine WARNUNG: das echte Host-Inventar
 * (welche Box hat wie viel Speicher) kommt erst mit PR 4 — bis dahin wäre ein
 * harter Block schlimmer als ein Hinweis, weil er korrekte Deploys verhindern
 * würde, sobald jemand eine grössere Kiste anschliesst.
 */
const SPARK_CLASS_USABLE_GB = 121;

/** Ab wann ein Eintrag nicht mehr „neu" ist — clientseitig aus first_seen_at. */
const NEW_WINDOW_DAYS = 7;

function isNew(recipe: LocalRecipe, now: number): boolean {
  if (!recipe.first_seen_at) return false;
  const seen = new Date(recipe.first_seen_at).getTime();
  if (Number.isNaN(seen)) return false;
  return now - seen < NEW_WINDOW_DAYS * 24 * 60 * 60 * 1000;
}

/**
 * Deploybar sind zwei Sorten, auf zwei verschiedenen Wegen:
 *   sparkrun     → Recipe-Switch auf einer bestehenden Spark-Runtime
 *   ssh_process  → eigener Dialog: installieren, Runtime anlegen, starten
 * Alles andere (vllm_docker/llamacpp_docker) läuft über den BoxWizard und
 * bleibt hier bewusst deaktiviert, statt einen Weg anzubieten, der scheitert.
 */
function isDeployable(recipe: LocalRecipe): boolean {
  if (recipe.engine === "ssh_process") return !!recipe.launch_template;
  return recipe.engine === "sparkrun" && !!recipe.recipe_ref;
}

// ── Karte ────────────────────────────────────────────────────────────────────

function RecipeCard({
  recipe,
  now,
  onDeploy,
  onToggleEnabled,
  toggling,
}: {
  recipe: LocalRecipe;
  now: number;
  onDeploy: (recipe: LocalRecipe) => void;
  onToggleEnabled: (recipe: LocalRecipe) => void;
  toggling: boolean;
}) {
  const t = useTranslations("runtimes.localRegistry");
  const fresh = isNew(recipe, now);
  const deployable = isDeployable(recipe);
  const tooBig =
    recipe.min_vram_gb != null && recipe.min_vram_gb > SPARK_CLASS_USABLE_GB;

  return (
    <ListRow
      testId="local-recipe-card"
      dataAttrs={{ "data-slug": recipe.slug, "data-engine": recipe.engine }}
      tone={recipe.running ? "ok" : "idle"}
      muted={!recipe.enabled}
      name={recipe.display_name}
      chips={
        <>
          {/* Page-wide chip order: state → type → size/detail. */}
          {fresh && (
            <MetaChip tone="accent" testId="local-registry-new-badge">
              {t("newBadge")}
            </MetaChip>
          )}
          {recipe.running && (
            <MetaChip
              tone="ok"
              icon={<CheckCircle2 size={10} />}
              title={t("runningTitle")}
              testId="local-registry-running-badge"
            >
              {t("runningBadge")}
            </MetaChip>
          )}
          {tooBig && (
            <MetaChip
              tone="warn"
              icon={<AlertTriangle size={10} />}
              title={t("fitWarningTitle", {
                needed: recipe.min_vram_gb ?? 0,
                limit: SPARK_CLASS_USABLE_GB,
              })}
              testId="local-registry-fit-warning"
            >
              {t("fitWarning")}
            </MetaChip>
          )}
          {recipe.gb10_validated && (
            <MetaChip tone="idle" title={t("gb10Title")} testId="local-registry-gb10-badge">
              {t("gb10Badge")}
            </MetaChip>
          )}
          <MetaChip tone="idle">{recipe.engine}</MetaChip>
          {recipe.quant && <MetaChip tone="idle">{recipe.quant}</MetaChip>}
          {recipe.arch !== "any" && <MetaChip tone="idle">{recipe.arch}</MetaChip>}
        </>
      }
      meta={
        <>
          <MetaText mono title={recipe.model_identifier}>
            {recipe.model_identifier}
          </MetaText>
          {recipe.est_weights_gb != null && (
            <MetaText className="tabular-nums shrink-0">
              {t("sizeGb", { n: recipe.est_weights_gb })}
            </MetaText>
          )}
          {recipe.context_len != null && (
            <MetaText className="tabular-nums shrink-0">
              {t("contextK", { n: Math.round(recipe.context_len / 1024) })}
            </MetaText>
          )}
          {/* Credit for the recipe author (PR #285) belongs to the row's meta
              line, not to a style of its own. */}
          {recipe.author && (
            <MetaText className="shrink-0">
              <span data-testid="local-registry-author">
                {t("byAuthor")}{" "}
                {recipe.author_url ? (
                  <a
                    href={recipe.author_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="underline underline-offset-2 inline-flex items-center gap-0.5"
                  >
                    {recipe.author}
                    <ExternalLink size={9} aria-hidden />
                  </a>
                ) : (
                  recipe.author
                )}
              </span>
            </MetaText>
          )}
        </>
      }
      action={
        <button
          type="button"
          data-testid="local-registry-deploy"
          onClick={() => onDeploy(recipe)}
          disabled={!deployable}
          title={deployable ? t("deployTitle", { name: recipe.display_name }) : t("deployUnavailableTitle")}
          className="inline-flex items-center gap-1 rounded-md px-2 py-1 min-h-7 font-mono uppercase text-[10px] tracking-[0.12em] cursor-pointer transition-colors bg-accent-subtle border border-accent text-accent disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <Rocket size={10} />
          {t("deploy")}
        </button>
      }
      overflow={
        <OverflowMenu
          label={t("rowActions", { name: recipe.display_name })}
          testId={`recipe-more-${recipe.slug}`}
          actions={[
            {
              id: "visibility",
              label: recipe.enabled ? t("hide") : t("unhide"),
              icon: recipe.enabled ? Eye : EyeOff,
              loading: toggling,
              onClick: () => onToggleEnabled(recipe),
            },
          ]}
        />
      }
    />
  );
}

// ── Ergebnis des Registry-Abgleichs ──────────────────────────────────────────

function RefreshSummary({ result }: { result: LocalRegistryRefreshResult }) {
  const t = useTranslations("runtimes.localRegistry");
  const [open, setOpen] = useState(false);
  const problem = result.failed > 0 || result.reasons.length > 0;

  return (
    <div
      data-testid="local-registry-refresh-result"
      className={`mb-3 rounded-md border px-2.5 py-2 text-[11px] text-secondary ${
        problem ? "bg-warn-subtle border-warn" : "bg-surface border-subtle"
      }`}
    >
      <div className="flex items-center gap-1.5">
        {problem ? (
          <AlertTriangle size={12} className="shrink-0 text-warn" />
        ) : (
          <CheckCircle2 size={12} className="shrink-0 text-ok" />
        )}
        <span>
          {t("refreshResult", {
            added: result.added,
            updated: result.updated,
            failed: result.failed,
          })}
        </span>
      </div>

      {result.reasons.length > 0 && (
        <>
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            className="mt-1 inline-flex items-center gap-1 text-[11px] text-muted cursor-pointer"
          >
            <ChevronRight
              size={11}
              className={`transition-transform ${open ? "rotate-90" : ""}`}
            />
            {t("refreshReasons", { n: result.reasons.length })}
          </button>
          {open && (
            <ul
              data-testid="local-registry-refresh-reasons"
              className="mt-1 flex flex-col gap-0.5 font-mono text-[10px] text-muted"
            >
              {result.reasons.map((r, i) => (
                <li key={i} className="break-words">
                  {r}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}

// ── Sektion ──────────────────────────────────────────────────────────────────

export function LocalModelBrowser({ embedded = false }: { embedded?: boolean } = {}) {
  const t = useTranslations("runtimes.localRegistry");
  const locale = useLocale();
  const queryClient = useQueryClient();
  const addNotification = useNotificationStore((s) => s.addNotification);

  const [showHidden, setShowHidden] = useState(false);
  const [search, setSearch] = useState("");
  const [engineFilter, setEngineFilter] = useState<string>("");
  const [pending, setPending] = useState<LocalRecipe | null>(null);
  const [installing, setInstalling] = useState<LocalRecipe | null>(null);
  const [targetRuntimeId, setTargetRuntimeId] = useState<string>("");
  const [togglingSlug, setTogglingSlug] = useState<string | null>(null);
  const [refreshResult, setRefreshResult] =
    useState<LocalRegistryRefreshResult | null>(null);

  // Nur `enabled` geht ans Backend: es entscheidet, ob ausgeblendete Einträge
  // überhaupt geladen werden. Suche und Engine-Filter bleiben clientseitig —
  // bei <50 Einträgen ist das sofort statt ein Request pro Tastendruck.
  const { data, isLoading, error } = useQuery({
    queryKey: ["local-registry", showHidden],
    queryFn: () =>
      api.localRegistry.list(showHidden ? undefined : { enabled: true }),
  });

  // Die /runtimes-Seite hält diese Query bereits — hier ist es ein Cache-Treffer.
  const runtimesQuery = useQuery({
    queryKey: ["runtimes"],
    queryFn: () => api.runtimes.list(),
  });

  const refreshMutation = useMutation({
    mutationFn: () => api.localRegistry.refresh(),
    onSuccess: (res) => {
      setRefreshResult(res);
      queryClient.invalidateQueries({ queryKey: ["local-registry"] });
    },
    onError: (err: Error) =>
      addNotification({
        type: "error",
        message: t("refreshFailedToast", { message: err.message }),
        persistent: false,
      }),
  });

  const enabledMutation = useMutation({
    mutationFn: ({ slug, enabled }: { slug: string; enabled: boolean }) =>
      api.localRegistry.setEnabled(slug, enabled),
    onSettled: () => setTogglingSlug(null),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["local-registry"] });
    },
    onError: (err: Error) =>
      addNotification({
        type: "error",
        message: t("hideFailedToast", { message: err.message }),
        persistent: false,
      }),
  });

  // Exakt der Pfad des SparkRecipeSwitcher — ein Deploy ist ein Recipe-Switch,
  // kein eigener Lifecycle.
  const deployMutation = useMutation({
    mutationFn: ({ runtimeId, recipe }: { runtimeId: string; recipe: string }) =>
      api.runtimes.sparkrun.switchRecipe(runtimeId, recipe),
    onSuccess: (_res, vars) => {
      const runtime = runtimesQuery.data?.runtimes.find((r) => r.id === vars.runtimeId);
      addNotification({
        type: "success",
        message: t("deployStarted", {
          name: pending?.display_name ?? vars.recipe,
          runtime: runtime?.display_name ?? vars.runtimeId,
        }),
        persistent: false,
      });
      setPending(null);
      queryClient.invalidateQueries({ queryKey: ["runtimes"] });
      queryClient.invalidateQueries({ queryKey: ["runtime-current-recipe"] });
      queryClient.invalidateQueries({ queryKey: ["local-registry"] });
    },
    onError: (err: Error) => {
      setPending(null);
      addNotification({
        type: "error",
        message: t("deployFailed", { message: err.message }),
        persistent: false,
      });
    },
  });

  const recipes = useMemo(() => data?.recipes ?? [], [data]);

  const engines = useMemo(
    () => Array.from(new Set(recipes.map((r) => r.engine))).sort(),
    [recipes],
  );

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return recipes.filter((r) => {
      if (engineFilter && r.engine !== engineFilter) return false;
      if (!needle) return true;
      return (
        r.display_name.toLowerCase().includes(needle) ||
        r.slug.toLowerCase().includes(needle) ||
        r.model_identifier.toLowerCase().includes(needle) ||
        r.tags.some((tag) => tag.toLowerCase().includes(needle))
      );
    });
  }, [recipes, search, engineFilter]);

  // Ein Zeitstempel pro Render statt Date.now() je Karte — sonst könnte die
  // 7-Tage-Grenze innerhalb einer Liste unterschiedlich ausfallen.
  const now = Date.now();
  const newCount = useMemo(
    () => visible.filter((r) => isNew(r, now)).length,
    [visible, now],
  );
  const lastUpdated = useMemo(
    () =>
      recipes
        .map((r) => r.updated_at)
        .filter((v): v is string => !!v)
        .sort()
        .pop() ?? null,
    [recipes],
  );

  const vllmRuntimes: Runtime[] = useMemo(
    () =>
      (runtimesQuery.data?.runtimes ?? []).filter(
        (r) => r.runtime_type === "vllm_docker",
      ),
    [runtimesQuery.data],
  );

  const openDeploy = (recipe: LocalRecipe) => {
    if (recipe.engine === "ssh_process") {
      setInstalling(recipe);
      return;
    }
    setPending(recipe);
    setTargetRuntimeId(vllmRuntimes[0]?.id ?? "");
  };

  return (
    <SectionOrFragment
      embedded={embedded}
      id="local-models"
      title={t("title")}
      hint={t("subtitle", { time: timeAgo(lastUpdated, locale) })}
      count={recipes.length}
      badge={
        newCount > 0 ? (
          <span
            data-testid="local-registry-new-count"
            className="label-sys text-accent border border-accent bg-accent-subtle rounded-sm px-1.5 py-px"
          >
            {newCount} {t("newLabel")}
          </span>
        ) : undefined
      }
      actions={
        <button
          type="button"
          onClick={() => refreshMutation.mutate()}
          disabled={refreshMutation.isPending}
          className="shrink-0 flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg border border-subtle bg-surface text-muted transition-all cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {refreshMutation.isPending ? (
            <Loader2 size={11} className="animate-spin" />
          ) : (
            <RotateCcw size={11} />
          )}
          {t("refreshNow")}
        </button>
      }
    >

      {/* Filterleiste — nur wenn es überhaupt etwas zu filtern gibt.
          Filter über einer leeren Registry sind reines Rauschen. */}
      {recipes.length > 0 && (
      <>
      {/* Filterleiste */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="relative min-w-[180px] flex-1">
          <Search
            size={12}
            className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-dim"
          />
          <input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label={t("searchLabel")}
            placeholder={t("searchPlaceholder")}
            className="w-full rounded-md border border-subtle bg-surface py-1.5 pl-7 pr-2.5 text-xs text-primary outline-none"
          />
        </div>
        <select
          value={engineFilter}
          onChange={(e) => setEngineFilter(e.target.value)}
          aria-label={t("engineFilterLabel")}
          className="rounded-md border border-subtle bg-surface px-2 py-1.5 text-xs text-muted cursor-pointer"
        >
          <option value="">{t("engineAll")}</option>
          {engines.map((e) => (
            <option key={e} value={e}>
              {e}
            </option>
          ))}
        </select>
        <label className="flex items-center gap-1.5 text-xs text-muted cursor-pointer">
          <input
            type="checkbox"
            checked={showHidden}
            onChange={(e) => setShowHidden(e.target.checked)}
            className="cursor-pointer"
          />
          {t("showHidden")}
        </label>
      </div>
      </>
      )}

      {refreshResult && <RefreshSummary result={refreshResult} />}

      {isLoading && (
        <div className="flex items-center gap-2 py-2 text-muted">
          <Loader2 size={13} className="animate-spin" />
          <span className="text-xs">{t("loading")}</span>
        </div>
      )}

      {error && (
        <div className="flex items-center gap-2 text-xs px-4 py-3 rounded-xl border border-err bg-err-subtle text-err">
          <AlertCircle size={13} />
          {t("loadError")}
        </div>
      )}

      {!isLoading && !error && recipes.length === 0 && (
        <div className="text-xs text-center py-10 text-muted">{t("empty")}</div>
      )}

      {!isLoading && !error && recipes.length > 0 && visible.length === 0 && (
        <div className="text-xs text-center py-10 text-muted">
          {t("emptyFiltered")}
        </div>
      )}

      {visible.length > 0 && (
        <CappedList testId="recipe-list" maxRows={6}>
          {visible.map((r) => (
            <RecipeCard
              key={r.slug}
              recipe={r}
              now={now}
              onDeploy={openDeploy}
              onToggleEnabled={(recipe) => {
                setTogglingSlug(recipe.slug);
                enabledMutation.mutate({
                  slug: recipe.slug,
                  enabled: !recipe.enabled,
                });
              }}
              toggling={togglingSlug === r.slug}
            />
          ))}
        </CappedList>
      )}

      <ConfirmDialog
        open={!!pending}
        danger={false}
        kicker={t("deployKicker")}
        title={pending ? pending.display_name : ""}
        confirmLabel={t("deployConfirm")}
        loading={deployMutation.isPending}
        body={
          pending ? (
            <div className="flex flex-col gap-2">
              <div>
                {t("deployBody")}{" "}
                <span className="font-mono text-primary">{pending.recipe_ref}</span>
              </div>
              {vllmRuntimes.length > 0 ? (
                <label className="flex flex-col gap-1">
                  <span className="label-sys label-sys--dim">
                    {t("deployTargetLabel")}
                  </span>
                  <select
                    value={targetRuntimeId}
                    onChange={(e) => setTargetRuntimeId(e.target.value)}
                    aria-label={t("deployTargetLabel")}
                    className="rounded-md border border-subtle bg-surface px-2 py-1.5 text-xs text-primary cursor-pointer"
                  >
                    {vllmRuntimes.map((rt) => (
                      <option key={rt.id} value={rt.id}>
                        {rt.display_name}
                      </option>
                    ))}
                  </select>
                </label>
              ) : (
                <div className="text-warn">{t("deployNoRuntimes")}</div>
              )}
              <div className="text-muted">{t("deployEvictHint")}</div>
            </div>
          ) : null
        }
        onConfirm={() => {
          if (!pending?.recipe_ref || !targetRuntimeId) return;
          deployMutation.mutate({
            runtimeId: targetRuntimeId,
            recipe: pending.recipe_ref,
          });
        }}
        onCancel={() => setPending(null)}
      />

      {installing && (
        <SshProcessDeployDialog
          recipe={installing}
          onClose={() => setInstalling(null)}
        />
      )}
    </SectionOrFragment>
  );
}

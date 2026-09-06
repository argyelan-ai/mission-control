"use client";

/**
 * RecipeGroup — Cockpit-Gruppe „Recipe" (Spec §4.5). Fakten-Liste model ·
 * source · env · worker. NUR Felder, die es wirklich gibt (HONESTY RULE):
 *
 * - `model`: `runtime.model_identifier`.
 * - `source`: aufgelöst über den Autostart-Status dieser Box
 *   (`HostAutostartStatus.recipe_slug` — dasselbe Rezept, das der Umschalter
 *   zuletzt hier gestartet hat) gegen `api.localRegistry.list()`
 *   (`LocalRecipe.source_registry`/`source_url`). Kein Commit-Hash im
 *   Vertrag — anders als das Mockup ("@ c2325b2") zeigt die Zeile nur, was
 *   wirklich da ist.
 * - `path`: ENTFÄLLT — weder `Runtime` noch `LocalRecipe` tragen einen
 *   Installationspfad. Das Mockup-Feld ("~/code/qwen38-flash-next") hat
 *   keine reale Quelle; erfinden verstösst gegen die Vorgabe im Auftrag.
 * - `env`: `LocalRecipe.env` (Auszug, erste Einträge).
 * - `worker`: die andere Box im Duo (Slug · Fabric-IP), falls vorhanden.
 *
 * Zeigt nichts, wenn die Runtime kein Rezept über den Umschalter gestartet
 * hat (kein `recipe_slug`) — dann bleiben nur `model`/`worker`, was ehrlich
 * ist: MC weiss dann schlicht nicht, woher die Runtime kommt.
 */

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import type { Host, Runtime } from "@/lib/types";
import { hostAutostartKey } from "../../hostAutostartKey";

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <>
      <span className="font-mono" style={{ fontSize: "11px", color: C.textMuted }}>{label}</span>
      <span className="font-mono truncate" style={{ fontSize: "11px", color: C.textSecondary }} title={value}>{value}</span>
    </>
  );
}

export function RecipeGroup({
  hostId,
  runtime,
  workerHost,
}: {
  hostId: string;
  runtime: Runtime;
  /** Die andere Box im Duo, falls vorhanden (Spec: „worker (IP, RoCE)"). */
  workerHost?: Host | null;
}) {
  const t = useTranslations("runtimes.cockpit");

  const { data: autostart } = useQuery({
    queryKey: hostAutostartKey(hostId),
    queryFn: () => api.hosts.autostart(hostId),
    staleTime: 15_000,
    retry: false,
  });
  const recipeSlug = autostart?.recipe_slug ?? null;

  const { data: registry } = useQuery({
    queryKey: ["local-registry"],
    queryFn: () => api.localRegistry.list(),
    enabled: !!recipeSlug,
    staleTime: 60_000,
    retry: false,
  });
  const recipe = recipeSlug ? registry?.recipes.find((r) => r.slug === recipeSlug) ?? null : null;

  const envEntries = recipe?.env ? Object.entries(recipe.env).slice(0, 4) : [];
  const source = recipe
    ? [recipe.source_registry, recipe.source_url].filter(Boolean).join(" · ")
    : null;

  return (
    <div className="grid gap-y-1.5" style={{ gridTemplateColumns: "auto 1fr", columnGap: "14px" }} data-testid="recipe-group">
      {runtime.model_identifier && <Fact label={t("factModel")} value={runtime.model_identifier} />}
      {source && <Fact label={t("factSource")} value={source} />}
      {envEntries.length > 0 && (
        <Fact label={t("factEnv")} value={envEntries.map(([k, v]) => `${k}=${v}`).join(" · ")} />
      )}
      {workerHost && (
        <Fact
          label={t("factWorker")}
          value={[workerHost.slug, workerHost.fabric_ip ?? workerHost.ssh_host].filter(Boolean).join(" · ")}
        />
      )}
      {!runtime.model_identifier && !source && envEntries.length === 0 && !workerHost && (
        <span className="col-span-2 text-[11px]" style={{ color: C.textMuted }} data-testid="recipe-group-empty">
          {t("recipeNoFacts")}
        </span>
      )}
    </div>
  );
}

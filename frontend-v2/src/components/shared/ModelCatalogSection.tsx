"use client";

/**
 * ModelCatalogSection — Anbieter-Modellkatalog auf /runtimes.
 *
 * Leitprinzip: Der Katalog sagt NUR „diese Modelle gibt es beim Anbieter",
 * niemals „dieses Modell läuft". Die laufende Wahrheit bleibt allein
 * runtime.model_identifier. Ein Katalog-Modell wird per „Als Runtime anlegen"
 * zu einer Runtime-Zeile — danach taucht es im bestehenden RuntimeSwitchModal
 * auf. Es gibt hier bewusst KEINEN zweiten Switch-Pfad und kein Modell-Dropdown
 * am Agent.
 *
 * Ehrliche Status-Darstellung ist der Kern: eine leere oder unvollständige
 * Liste wird nie als „keine Modelle" getarnt — jeder Fehlerzustand nennt
 * seinen Grund.
 *
 * Struktur folgt der CliToolsSection (Fleet-Sektion + „Update verfügbar"-Badge
 * + Aktions-Button), nur mit Modellen statt CLI-Binaries. Styling: ausschliesslich
 * semantische Klassen aus globals.css, keine lib/colors.ts-Konstanten, kein
 * inline style={{}} — damit die anstehende Kontrast-Nachkalibrierung der Tokens
 * automatisch durchzieht.
 */

import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import {
  RotateCcw,
  Loader2,
  AlertCircle,
  AlertTriangle,
  PlugZap,
  KeyRound,
  ChevronRight,
  Plus,
  CheckCircle2,
  FileCode2,
  Terminal,
} from "lucide-react";
import { api } from "@/lib/api";
import type {
  ModelCatalogModel,
  ModelCatalogProvider,
  ModelCatalogStatus,
} from "@/lib/types";
import { useNotificationStore } from "@/lib/store";
import { timeAgo } from "@/lib/utils";
import { SectionOrFragment } from "@/components/shared/Section";
import { CappedList } from "@/components/shared/CappedList";
import { ListRow, MetaChip, MetaText, RowAction } from "@/components/shared/ListRow";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";

// ── Status-Vokabular ─────────────────────────────────────────────────────────
// Jeder Zustand bekommt eigene Worte. „unreachable" darf NIE wie „leer"
// aussehen, „credential_missing" nie wie „Anbieter hat nichts".

interface StatusChrome {
  /** Kurzlabel in der Kopfzeile. */
  labelKey: string;
  /** Erklärender Satz im Hinweisband (null = kein Band, alles in Ordnung). */
  headlineKey: string | null;
  tone: "ok" | "warn" | "err";
  Icon: typeof AlertTriangle;
}

// labelKey pattern (docs/i18n.md): resolved via t() at the render site.
const STATUS_CHROME: Record<ModelCatalogStatus, StatusChrome> = {
  ok: {
    labelKey: "statusOkLabel",
    headlineKey: null,
    tone: "ok",
    Icon: CheckCircle2,
  },
  manifest_fallback: {
    labelKey: "statusManifestFallbackLabel",
    headlineKey: "statusManifestFallbackHeadline",
    tone: "warn",
    Icon: AlertTriangle,
  },
  cli_config: {
    labelKey: "statusCliConfigLabel",
    headlineKey: "statusCliConfigHeadline",
    tone: "warn",
    Icon: FileCode2,
  },
  credential_missing: {
    labelKey: "statusCredentialMissingLabel",
    headlineKey: "statusCredentialMissingHeadline",
    tone: "err",
    Icon: KeyRound,
  },
  unreachable: {
    labelKey: "statusUnreachableLabel",
    headlineKey: "statusUnreachableHeadline",
    tone: "err",
    Icon: PlugZap,
  },
};

const TONE_TEXT: Record<StatusChrome["tone"], string> = {
  ok: "text-ok",
  warn: "text-warn",
  err: "text-err",
};

const TONE_BANNER: Record<StatusChrome["tone"], string> = {
  ok: "bg-surface border-subtle",
  warn: "bg-warn-subtle border-warn",
  err: "bg-err-subtle border-err",
};

// ── Modell-Zeile ─────────────────────────────────────────────────────────────

function ModelRow({
  model,
  onBind,
  binding,
}: {
  model: ModelCatalogModel;
  onBind: (model: ModelCatalogModel) => void;
  binding: boolean;
}) {
  const t = useTranslations("runtimes.modelCatalog");
  // cli_only: existiert nur im CLI selbst, der Provider-Endpoint lehnt es ab.
  // Es wird gezeigt (der Operator soll wissen, dass es das Modell gibt), aber
  // NICHT als „neu" beworben und nicht zum Anlegen angeboten — das Backend
  // würde den Bind ohnehin mit 422 ablehnen. Eine Option anzubieten, die dann
  // scheitert, wäre schlechter als sie wegzulassen.
  const isCliOnly = model.cli_only === true;
  const isNew = model.bound === false && !isCliOnly;

  return (
    <ListRow
      testId="catalog-model-row"
      dataAttrs={{
        role: "listitem",
        "data-bound": model.bound ? "true" : "false",
        "data-cli-only": isCliOnly ? "true" : "false",
      }}
      tone={isNew ? "accent" : "idle"}
      name={
        <span
          className="font-mono text-[11px]"
          title={
            model.display_name && model.display_name !== model.id
              ? `${model.id} · ${model.display_name}`
              : model.id
          }
        >
          {model.id}
        </span>
      }
      summary={
        model.context_window != null
          ? t("contextK", { n: Math.round(model.context_window / 1024) })
          : undefined
      }
      chips={
        <>
          {isCliOnly && (
            <MetaChip
              tone="idle"
              icon={<Terminal size={10} />}
              title={model.note ?? t("cliOnlyTitle")}
              testId="catalog-cli-only-badge"
            >
              {t("cliOnlyBadge")}
            </MetaChip>
          )}
          {isNew && (
            <MetaChip tone="accent" testId="catalog-new-badge">
              {t("newBadge")}
            </MetaChip>
          )}
          {!isNew && !isCliOnly && (
            <MetaChip tone="idle" title={t("boundTitle")}>
              {t("boundBadge")}
            </MetaChip>
          )}
        </>
      }
      meta={
        model.context_window != null ? (
          <MetaText className="tabular-nums shrink-0">
            {t("contextK", { n: Math.round(model.context_window / 1024) })}
          </MetaText>
        ) : undefined
      }
      action={
        isNew ? (
          <RowAction
            icon={binding ? <Loader2 size={10} className="animate-spin" /> : <Plus size={10} />}
            onClick={() => onBind(model)}
            disabled={binding}
            title={t("createRuntimeRowTitle", { id: model.id })}
          >
            {t("createAsRuntime")}
          </RowAction>
        ) : undefined
      }
    />
  );
}

// ── Anbieter-Gruppe ──────────────────────────────────────────────────────────

function ProviderGroup({
  provider,
  onBind,
  bindingModelId,
}: {
  provider: ModelCatalogProvider;
  onBind: (provider: ModelCatalogProvider, model: ModelCatalogModel) => void;
  bindingModelId: string | null;
}) {
  const t = useTranslations("runtimes.modelCatalog");
  const locale = useLocale();
  const models = provider.models ?? [];
  const newCount = provider.new_count ?? models.filter((m) => !m.bound).length;
  const chrome = STATUS_CHROME[provider.status] ?? STATUS_CHROME.ok;

  // „Keine Modelle" ist eine AUSSAGE — sie darf nur fallen, wenn die Probe
  // wirklich durchlief. Bei jedem Fehlerzustand spricht stattdessen das Band.
  const probeSucceeded = provider.status === "ok";

  // Standard: aufgeklappt, wenn es etwas Neues gibt ODER etwas nicht stimmt —
  // sonst bleibt die Seite ruhig (Anthropic allein liefert 11 Modelle).
  const [open, setOpen] = useState(newCount > 0 || !probeSucceeded);

  return (
    <div
      data-testid="catalog-provider"
      data-provider={provider.key}
      data-status={provider.status}
      className="rounded-lg border border-subtle bg-elevated overflow-hidden"
    >
      {/* Kopfzeile — eine Zeile: Status, Name, Protokoll, Anzahl, Prüfzeit. */}
      <ListRow
        testId="catalog-provider-row"
        tone={chrome.tone === "ok" ? "ok" : chrome.tone === "warn" ? "warn" : "error"}
        className="!border-transparent !bg-transparent"
        onClick={() => setOpen((v) => !v)}
        leading={
          <ChevronRight
            size={13}
            className={`shrink-0 text-dim transition-transform ${open ? "rotate-90" : ""}`}
          />
        }
        name={provider.display_name || provider.key}
        summary={[provider.protocol, `${models.length}`, t(chrome.labelKey)].join(" · ")}
        chips={
          <>
            <MetaChip tone="idle">{provider.protocol}</MetaChip>
            <MetaChip tone="idle" className="tabular-nums">
              {models.length}
            </MetaChip>
            {newCount > 0 && (
              <MetaChip tone="accent" testId="catalog-new-count">
                {newCount} {t("newLabel")}
              </MetaChip>
            )}
          </>
        }
        meta={
          <>
            <MetaText className="hidden sm:inline shrink-0" title={t(chrome.labelKey)}>
              {t("checked", { time: timeAgo(provider.cached_at, locale) })}
            </MetaText>
            {provider.endpoint && (
              <MetaText mono className="hidden sm:inline max-w-[220px]" title={provider.endpoint}>
                {provider.endpoint}
              </MetaText>
            )}
          </>
        }
      />

      {/* Ehrliches Status-Band — nennt Zustand UND Grund */}
      {chrome.headlineKey && (
        <div
          data-testid="catalog-status-banner"
          className={`mx-3 mb-2.5 flex items-start gap-2 rounded-md border px-2.5 py-2 text-[11px] text-secondary ${TONE_BANNER[chrome.tone]}`}
        >
          <chrome.Icon size={12} className={`mt-px shrink-0 ${TONE_TEXT[chrome.tone]}`} />
          <div className="min-w-0">
            <div>{t(chrome.headlineKey)}</div>
            {provider.reason && (
              <div
                data-testid="catalog-status-reason"
                className="mt-1 font-mono text-[10px] text-muted break-words"
              >
                {provider.reason}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Modelle */}
      {open && (
        <div className="px-3 pb-3">
          {models.length > 0 ? (
            // Anthropic alone lists eleven models; showing every one of them
            // by default made an expanded provider taller than the rest of the
            // page put together.
            <CappedList
              maxRows={6}
              role="list"
              // This list sits on the provider card, not on the page canvas.
              fadeTo="var(--color-bg-elevated)"
              className="gap-1"
              testId={`catalog-models-${provider.key}`}
            >
              {models.map((m) => (
                <ModelRow
                  key={m.id}
                  model={m}
                  binding={bindingModelId === `${provider.key}::${m.id}`}
                  onBind={(model) => onBind(provider, model)}
                />
              ))}
            </CappedList>
          ) : probeSucceeded ? (
            <div className="py-3 text-center text-[11px] text-muted">
              {t("providerReachableNoModels")}
            </div>
          ) : (
            <div className="py-3 text-center text-[11px] text-muted">
              {t("noListAvailable")}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Sektion ──────────────────────────────────────────────────────────────────

interface PendingBind {
  provider: ModelCatalogProvider;
  model: ModelCatalogModel;
}

export function ModelCatalogSection({ embedded = false }: { embedded?: boolean } = {}) {
  const t = useTranslations("runtimes.modelCatalog");
  const locale = useLocale();
  const queryClient = useQueryClient();
  const addNotification = useNotificationStore((s) => s.addNotification);
  const [pending, setPending] = useState<PendingBind | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["model-catalog"],
    queryFn: () => api.modelCatalog.list(),
    refetchInterval: 120_000,
  });

  const refreshMutation = useMutation({
    mutationFn: () => api.modelCatalog.refresh(),
    onSuccess: (res) => {
      queryClient.setQueryData(["model-catalog"], res);
    },
    onError: (err: Error) =>
      addNotification({
        type: "error",
        message: t("refreshFailedToast", { message: err.message }),
        persistent: false,
      }),
  });

  const bindMutation = useMutation({
    mutationFn: ({ provider, model }: PendingBind) =>
      api.modelCatalog.bind(provider.key, model.id),
    onSuccess: (res, vars) => {
      setPending(null);
      // Die neue Zeile muss sofort in der Runtime-Liste dieser Seite stehen —
      // erst dadurch erscheint sie im bestehenden RuntimeSwitchModal.
      queryClient.invalidateQueries({ queryKey: ["runtimes"] });
      queryClient.invalidateQueries({ queryKey: ["model-catalog"] });
      const slug = res?.slug;
      addNotification({
        type: "success",
        message: res?.created === false
          ? t("runtimeAlreadyExisted", { id: vars.model.id, slugSuffix: slug ? ` (${slug})` : "" })
          : t("runtimeCreated", { name: slug ?? vars.model.id }),
        persistent: false,
      });
    },
    onError: (err: Error) => {
      setPending(null);
      // 409 = Slug-Kollision mit einem ANDEREN Modell — kein stiller Fehlschlag.
      addNotification({
        type: "error",
        message: err.message.includes("409")
          ? t("slugCollision", { message: err.message })
          : t("createFailedToast", { message: err.message }),
        persistent: false,
      });
    },
  });

  const providers = data?.providers ?? [];
  const totalNew = useMemo(
    () => providers.reduce((sum, p) => sum + (p.new_count ?? 0), 0),
    [providers],
  );
  const newestCachedAt = useMemo(
    () =>
      providers
        .map((p) => p.cached_at)
        .filter((v): v is string => !!v)
        .sort()
        .pop() ?? null,
    [providers],
  );

  const bindingKey = bindMutation.isPending && pending
    ? `${pending.provider.key}::${pending.model.id}`
    : null;

  return (
    <SectionOrFragment
      embedded={embedded}
      // The Models tab strip already names and counts this surface.
      embeddedTitle={false}
      id="model-catalog"
      title={t("title")}
      hint={t("subtitle", { time: timeAgo(newestCachedAt, locale) })}
      count={providers.length}
      badge={
        totalNew > 0 ? (
          <span
            data-testid="catalog-total-new"
            className="label-sys text-accent border border-accent bg-accent-subtle rounded-sm px-1.5 py-0.5"
          >
            {totalNew} {t("newLabel")}
          </span>
        ) : undefined
      }
      actions={
        <button
          type="button"
          onClick={() => refreshMutation.mutate()}
          disabled={refreshMutation.isPending}
          className="shrink-0 flex items-center gap-1.5 text-xs px-3 py-2 sm:py-1.5 min-h-11 sm:min-h-0 rounded-md border border-subtle bg-surface text-muted transition-all cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
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

      {!isLoading && !error && providers.length === 0 && (
        <div className="text-xs text-center py-10 text-muted">
          {t("noProvidersConfigured")}
        </div>
      )}

      {providers.length > 0 && (
        <CappedList testId="catalog-list" maxRows={2}>
          {providers.map((p) => (
            <ProviderGroup
              key={p.key}
              provider={p}
              bindingModelId={bindingKey}
              onBind={(provider, model) => setPending({ provider, model })}
            />
          ))}
        </CappedList>
      )}

      <ConfirmDialog
        open={!!pending}
        danger={false}
        kicker={t("confirmKicker")}
        title={pending ? pending.model.id : ""}
        confirmLabel={t("confirmCreate")}
        loading={bindMutation.isPending}
        body={
          pending ? (
            <div className="flex flex-col gap-2">
              <div>
                {t("confirmBodyBefore")}{" "}
                <span className="font-mono text-primary">{pending.model.id}</span> {t("confirmBodyAt")}{" "}
                <span className="text-primary">{pending.provider.display_name}</span>{t("confirmBodyAfter")}
              </div>
              <div className="text-muted">
                {t("confirmBodyHint")}
              </div>
            </div>
          ) : null
        }
        onConfirm={() => pending && bindMutation.mutate(pending)}
        onCancel={() => setPending(null)}
      />
    </SectionOrFragment>
  );
}

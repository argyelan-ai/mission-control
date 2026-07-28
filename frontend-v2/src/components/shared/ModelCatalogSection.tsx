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
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";

// ── Status-Vokabular ─────────────────────────────────────────────────────────
// Jeder Zustand bekommt eigene Worte. „unreachable" darf NIE wie „leer"
// aussehen, „credential_missing" nie wie „Anbieter hat nichts".

interface StatusChrome {
  /** Kurzlabel in der Kopfzeile. */
  label: string;
  /** Erklärender Satz im Hinweisband (null = kein Band, alles in Ordnung). */
  headline: string | null;
  tone: "ok" | "warn" | "err";
  Icon: typeof AlertTriangle;
}

const STATUS_CHROME: Record<ModelCatalogStatus, StatusChrome> = {
  ok: {
    label: "live",
    headline: null,
    tone: "ok",
    Icon: CheckCircle2,
  },
  manifest_fallback: {
    label: "Manifest-Fallback",
    headline:
      "Live-Abfrage fehlgeschlagen, zeige bekannte Liste — sie kann veraltet sein.",
    tone: "warn",
    Icon: AlertTriangle,
  },
  cli_config: {
    label: "aus CLI-Config",
    headline:
      "Live-Abfrage fehlgeschlagen — die Liste kommt aus der Config des CLI selbst (dieselbe Datei, die das Tool zur Modellwahl liest). Aktuell, aber nicht vom Anbieter bestätigt.",
    tone: "warn",
    Icon: FileCode2,
  },
  credential_missing: {
    label: "Zugangsdaten fehlen",
    headline:
      "Die Zugangsdaten sind für das Backend nicht erreichbar — der Anbieter konnte nicht abgefragt werden. Das ist keine leere Modell-Liste.",
    tone: "err",
    Icon: KeyRound,
  },
  unreachable: {
    label: "Runtime offline",
    headline:
      "Runtime offline — der Endpoint antwortet nicht. Ob es dort Modelle gibt, ist unbekannt.",
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
  // cli_only: existiert nur im CLI selbst, der Provider-Endpoint lehnt es ab.
  // Es wird gezeigt (der Operator soll wissen, dass es das Modell gibt), aber
  // NICHT als „neu" beworben und nicht zum Anlegen angeboten — das Backend
  // würde den Bind ohnehin mit 422 ablehnen. Eine Option anzubieten, die dann
  // scheitert, wäre schlechter als sie wegzulassen.
  const isCliOnly = model.cli_only === true;
  const isNew = model.bound === false && !isCliOnly;

  return (
    <li
      data-testid="catalog-model-row"
      data-bound={model.bound ? "true" : "false"}
      data-cli-only={isCliOnly ? "true" : "false"}
      className={`flex items-center gap-2 px-2.5 py-1.5 rounded-md border ${
        isNew ? "bg-surface border-subtle" : "border-transparent"
      }`}
    >
      <div className="min-w-0 flex-1">
        <div
          className={`font-mono text-[11px] truncate ${
            isNew ? "text-primary" : "text-muted"
          }`}
          title={model.id}
        >
          {model.id}
        </div>
        {(model.display_name || model.context_window) && (
          <div className="text-[10px] truncate text-dim">
            {model.display_name !== model.id ? model.display_name : null}
            {model.display_name && model.display_name !== model.id && model.context_window
              ? " · "
              : null}
            {model.context_window
              ? `${Math.round(model.context_window / 1024)}k Kontext`
              : null}
          </div>
        )}
      </div>

      {isCliOnly ? (
        <span
          data-testid="catalog-cli-only-badge"
          title={
            model.note ??
            "Nur im CLI selbst wählbar — der HTTP-Endpoint des Anbieters kennt dieses Modell nicht. Als Runtime nicht anlegbar."
          }
          className="shrink-0 inline-flex items-center gap-1 label-sys text-dim border border-subtle rounded-sm px-1.5 py-px"
        >
          <Terminal size={10} />
          nur im CLI
        </span>
      ) : isNew ? (
        <>
          <span
            data-testid="catalog-new-badge"
            className="shrink-0 label-sys text-accent border border-accent bg-accent-subtle rounded-sm px-1.5 py-px"
          >
            neu
          </span>
          <button
            type="button"
            onClick={() => onBind(model)}
            disabled={binding}
            title={`Runtime-Zeile für ${model.id} anlegen`}
            className="shrink-0 inline-flex items-center gap-1 rounded-sm px-2 py-1 font-mono uppercase text-[10px] tracking-[0.12em] cursor-pointer transition-colors bg-accent-subtle border border-accent text-accent disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {binding ? (
              <Loader2 size={10} className="animate-spin" />
            ) : (
              <Plus size={10} />
            )}
            Als Runtime anlegen
          </button>
        </>
      ) : (
        <span className="shrink-0 label-sys" title="Es existiert bereits eine Runtime-Zeile für dieses Modell">
          gebunden
        </span>
      )}
    </li>
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
      {/* Kopfzeile */}
      <div className="flex items-center gap-2 px-3 py-2.5">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex min-w-0 flex-1 items-center gap-2 text-left cursor-pointer"
        >
          <ChevronRight
            size={13}
            className={`shrink-0 text-dim transition-transform ${open ? "rotate-90" : ""}`}
          />
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium text-primary truncate">
                {provider.display_name || provider.key}
              </span>
              <span className="label-sys shrink-0">{provider.protocol}</span>
              {newCount > 0 && (
                <span
                  data-testid="catalog-new-count"
                  className="shrink-0 label-sys text-accent border border-accent bg-accent-subtle rounded-sm px-1.5 py-px"
                >
                  {newCount} neu
                </span>
              )}
            </div>
            <div className="mt-0.5 flex items-center gap-1.5 text-[11px]">
              <chrome.Icon size={11} className={`shrink-0 ${TONE_TEXT[chrome.tone]}`} />
              <span className={TONE_TEXT[chrome.tone]}>{chrome.label}</span>
              <span className="text-dim">·</span>
              <span className="text-muted">geprüft {timeAgo(provider.cached_at)}</span>
            </div>
          </div>
        </button>

        {provider.endpoint && (
          <span
            className="hidden sm:block shrink-0 max-w-[220px] truncate font-mono text-[10px] text-dim"
            title={provider.endpoint}
          >
            {provider.endpoint}
          </span>
        )}
      </div>

      {/* Ehrliches Status-Band — nennt Zustand UND Grund */}
      {chrome.headline && (
        <div
          data-testid="catalog-status-banner"
          className={`mx-3 mb-2.5 flex items-start gap-2 rounded-md border px-2.5 py-2 text-[11px] text-secondary ${TONE_BANNER[chrome.tone]}`}
        >
          <chrome.Icon size={12} className={`mt-px shrink-0 ${TONE_TEXT[chrome.tone]}`} />
          <div className="min-w-0">
            <div>{chrome.headline}</div>
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
            <ul className="flex flex-col gap-1">
              {models.map((m) => (
                <ModelRow
                  key={m.id}
                  model={m}
                  binding={bindingModelId === `${provider.key}::${m.id}`}
                  onBind={(model) => onBind(provider, model)}
                />
              ))}
            </ul>
          ) : probeSucceeded ? (
            <div className="py-3 text-center text-[11px] text-muted">
              Anbieter erreichbar — meldet aktuell keine Modelle.
            </div>
          ) : (
            <div className="py-3 text-center text-[11px] text-muted">
              Keine Liste verfügbar — siehe Hinweis oben.
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

export function ModelCatalogSection() {
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
        message: `Katalog-Refresh fehlgeschlagen: ${err.message}`,
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
          ? `Runtime für ${vars.model.id} bestand bereits${slug ? ` (${slug})` : ""}.`
          : `Runtime angelegt: ${slug ?? vars.model.id} — jetzt im Runtime-Switch wählbar.`,
        persistent: false,
      });
    },
    onError: (err: Error) => {
      setPending(null);
      // 409 = Slug-Kollision mit einem ANDEREN Modell — kein stiller Fehlschlag.
      addNotification({
        type: "error",
        message: err.message.includes("409")
          ? `Slug-Kollision: Es existiert bereits eine Runtime mit diesem Namen für ein anderes Modell. ${err.message}`
          : `Anlegen fehlgeschlagen: ${err.message}`,
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
    <div className="mt-8">
      {/* Sektions-Kopf — spiegelt die CLI-Tools-/vLLM-Header dieser Seite */}
      <div className="flex items-center gap-3 mb-4">
        <div
          aria-hidden
          className="w-px self-stretch min-h-[36px] bg-gradient-to-b from-[var(--color-accent)] to-transparent"
        />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="text-sm font-semibold text-primary">Modell-Katalog</h2>
            <span className="label-sys rounded-sm bg-surface px-1.5 py-px">Anbieter</span>
            {totalNew > 0 && (
              <span
                data-testid="catalog-total-new"
                className="label-sys text-accent border border-accent bg-accent-subtle rounded-sm px-1.5 py-px"
              >
                {totalNew} neu
              </span>
            )}
          </div>
          <p className="text-xs mt-0.5 text-muted">
            Was es bei den Anbietern gibt · geprüft {timeAgo(newestCachedAt)}
          </p>
        </div>
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
          Neu abfragen
        </button>
      </div>

      {isLoading && (
        <div className="flex items-center gap-2 py-2 text-muted">
          <Loader2 size={13} className="animate-spin" />
          <span className="text-xs">Katalog wird geladen...</span>
        </div>
      )}

      {error && (
        <div className="flex items-center gap-2 text-xs px-4 py-3 rounded-xl border border-err bg-err-subtle text-err">
          <AlertCircle size={13} />
          Modell-Katalog konnte nicht geladen werden.
        </div>
      )}

      {!isLoading && !error && providers.length === 0 && (
        <div className="text-xs text-center py-10 text-muted">
          Keine Anbieter konfiguriert.
        </div>
      )}

      {providers.length > 0 && (
        <div className="flex flex-col gap-2">
          {providers.map((p) => (
            <ProviderGroup
              key={p.key}
              provider={p}
              bindingModelId={bindingKey}
              onBind={(provider, model) => setPending({ provider, model })}
            />
          ))}
        </div>
      )}

      <ConfirmDialog
        open={!!pending}
        danger={false}
        kicker="Runtime anlegen"
        title={pending ? pending.model.id : ""}
        confirmLabel="Anlegen"
        loading={bindMutation.isPending}
        body={
          pending ? (
            <div className="flex flex-col gap-2">
              <div>
                Legt eine Runtime-Zeile für{" "}
                <span className="font-mono text-primary">{pending.model.id}</span> bei{" "}
                <span className="text-primary">{pending.provider.display_name}</span> an.
              </div>
              <div className="text-muted">
                Das startet nichts und schaltet keinen Agenten um — die Zeile steht
                danach im normalen Runtime-Switch zur Auswahl.
              </div>
            </div>
          ) : null
        }
        onConfirm={() => pending && bindMutation.mutate(pending)}
        onCancel={() => setPending(null)}
      />
    </div>
  );
}

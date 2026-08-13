"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { motion } from "framer-motion";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  ArrowDownToLine,
  Check,
  Clock,
  ExternalLink,
  Loader2,
  Plus,
  Power,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import type {
  Host,
  Runtime,
  RuntimeLiveStatus,
  HFRepoInfo,
  LMSCatalogModel,
  LMStudioModelsResponse,
} from "@/lib/types";
import AppShell from "@/components/layout/AppShell";
import { RuntimeScheduleTab } from "./RuntimeScheduleTab";
import { VllmContainerCatalog } from "./VllmContainerCatalog";
import { AddRuntimeModal } from "./AddRuntimeModal";
import { HostsSection } from "./HostsSection";
import { CliToolsSection } from "@/components/shared/CliToolsSection";
import { ModelCatalogSection } from "@/components/shared/ModelCatalogSection";
import { LocalModelBrowser } from "@/components/shared/LocalModelBrowser";
import { C, STATUS_TEXT } from "@/lib/colors";
import { EntityIcon } from "@/components/shared/EntityIcon";
import { Section, requestSectionOpen } from "@/components/shared/Section";
import { ListRow, MetaChip, MetaText, RowAction } from "@/components/shared/ListRow";
import { groupRuntimes, panelCapabilities, pickServing, type HostGroup } from "./grouping";
import { SlotStage } from "./SlotStage";
import { CloudUsage } from "./CloudUsage";
import { RuntimeDetailPanel } from "./RuntimeDetailPanel";

// ── Active Downloads Panel ────────────────────────────────────────────────────

function ActiveDownloads() {
  const t = useTranslations("runtimes");
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: ["lms-downloads"],
    queryFn: () => api.lmstudio.downloads(),
    refetchInterval: 4_000,
  });

  const cancelMutation = useMutation({
    mutationFn: (modelName: string) => api.lmstudio.cancelDownload(modelName),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["lms-downloads"] }),
  });

  const downloads = data?.downloads ?? [];
  if (downloads.length === 0) return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: -6 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -6 }}
      transition={{ duration: 0.2 }}
      className="mb-4"
    >
      <div className="flex items-center gap-2 mb-2 px-0.5">
        <span className="text-xs font-medium tracking-wider uppercase" style={{ color: C.warning, letterSpacing: "0.07em", fontSize: "10px" }}>
          {t("downloads")}
        </span>
        <div className="flex-1 h-px" style={{ background: `${C.warning}33` }} />
        <Loader2 size={10} className="animate-spin" style={{ color: C.warning }} />
      </div>
      <div className="flex flex-col gap-2">
        {downloads.map((dl) => (
          <div
            key={dl.id}
            style={{
              background: `${C.warning}0A`,
              border: `1px solid ${C.warning}26`,
              borderLeft: `1px solid ${C.warning}`,
              borderRadius: "10px",
              padding: "10px 12px",
            }}
          >
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium truncate" style={{ color: C.textPrimary }}>
                  {dl.name}
                </div>
                <div className="text-xs mt-0.5 truncate" style={{ color: C.textMuted }}>
                  {dl.type === "huggingface" && dl.repo ? `HuggingFace · ${dl.repo}` : "LM Studio"}
                  {dl.progress_text ? ` · ${dl.progress_text}` : ""}
                </div>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                {dl.progress_pct != null && (
                  <span className="text-sm font-semibold tabular-nums" style={{ color: C.warning }}>
                    {dl.progress_pct}%
                  </span>
                )}
                <button
                  onClick={() => cancelMutation.mutate(dl.name)}
                  disabled={cancelMutation.isPending && cancelMutation.variables === dl.name}
                  title={t("cancel")}
                  aria-label={t("cancelDownloadAria")}
                  className="flex items-center justify-center w-11 h-11 sm:w-7 sm:h-7 min-w-11 sm:min-w-[28px] cursor-pointer disabled:opacity-40"
                >
                  <span
                    aria-hidden
                    className="action-btn flex items-center justify-center w-7 h-7 rounded-md transition-colors"
                    style={{
                      background: "transparent",
                      border: `1px solid ${C.error}33`,
                      color: STATUS_TEXT.error,
                      ["--action-hover" as string]: `${C.error}1A`,
                    }}
                  >
                  {cancelMutation.isPending && cancelMutation.variables === dl.name
                    ? <Loader2 size={10} className="animate-spin" />
                    : <X size={12} />
                  }
                  </span>
                </button>
              </div>
            </div>
            {dl.progress_pct != null && (
              <div className="mt-2 h-0.5 rounded-full overflow-hidden" style={{ background: C.border }}>
                <motion.div
                  className="h-full rounded-full"
                  style={{ background: C.warning }}
                  initial={{ width: 0 }}
                  animate={{ width: `${dl.progress_pct}%` }}
                  transition={{ duration: 0.6, ease: "easeOut" }}
                />
              </div>
            )}
          </div>
        ))}
      </div>
    </motion.div>
  );
}

// ── Quantization Picker ─────────────────────────────────────────────────────

function QuantPicker({ modelId, onDownload, isPending }: {
  modelId: string;
  onDownload: (quant: string) => void;
  isPending: boolean;
}) {
  const t = useTranslations("runtimes.catalog");
  const { data, isFetching } = useQuery<HFRepoInfo>({
    queryKey: ["hf-files", modelId],
    queryFn: () => api.lmstudio.hfFiles(modelId),
  });

  const extractQuant = (filename: string): string => {
    const m = filename.match(/[-_](Q\d[^.]+)\.gguf$/i);
    return m ? m[1].toLowerCase() : filename.replace(/\.gguf$/i, "").split("-").pop() ?? "";
  };

  return (
    <div
      className="mx-3 mb-2 mt-1 rounded-lg overflow-hidden"
      style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}` }}
    >
      {isFetching ? (
        <div className="flex items-center gap-2 px-3 py-2.5 text-xs" style={{ color: C.textMuted }}>
          <Loader2 size={11} className="animate-spin" /> {t("loadingVariants")}
        </div>
      ) : data?.error ? (
        <div className="px-3 py-2 text-xs" style={{ color: STATUS_TEXT.error }}>{data.error}</div>
      ) : data?.files?.length ? (
        data.files.map((f, i) => {
          const quant = extractQuant(f.filename);
          return (
            <div
              key={f.filename}
              className="flex items-center justify-between px-3 py-2"
              style={{ borderTop: i > 0 ? `1px solid ${C.borderAccent}` : undefined }}
            >
              <div>
                <span className="text-xs font-mono" style={{ color: C.textPrimary }}>
                  {quant.toUpperCase()}
                </span>
                <span className="text-xs ml-2" style={{ color: C.textMuted }}>
                  {f.size_gb} GB
                </span>
              </div>
              <button
                onClick={() => onDownload(quant)}
                disabled={isPending}
                aria-label={t("download")}
                title={t("download")}
                className="flex items-center justify-center w-11 h-11 sm:w-7 sm:h-7 min-w-11 sm:min-w-[28px] cursor-pointer disabled:opacity-40"
              >
                <span
                  aria-hidden
                  className="flex items-center justify-center w-7 h-7 rounded-md"
                  style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.accent }}
                >
                  <ArrowDownToLine size={12} />
                </span>
              </button>
            </div>
          );
        })
      ) : (
        <div className="px-3 py-2 text-xs" style={{ color: C.textMuted }}>{t("noVariants")}</div>
      )}
    </div>
  );
}

// ── Model Catalog (search + download) ─────────────────────────────────────────

function ModelCatalog() {
  const t = useTranslations("runtimes.catalog");
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<"lms" | "hf">("lms");
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [isError, setIsError] = useState(false);
  const [pickingModel, setPickingModel] = useState<string | null>(null); // model_id for the quantization picker

  const { data: installedData } = useQuery<LMStudioModelsResponse>({
    queryKey: ["lms-models"],
    queryFn: api.lmstudio.list,
  });
  const installedIds = installedData?.models.map((m) => m.id) ?? [];

  const { data: catalogData, isFetching: catalogLoading } = useQuery<{ models: LMSCatalogModel[] }>({
    queryKey: ["lms-catalog", submitted],
    queryFn: () => api.lmstudio.catalogSearch(submitted),
    enabled: tab === "lms" && submitted.length > 0,
  });

  const { data: hfData, isFetching: hfLoading } = useQuery<HFRepoInfo>({
    queryKey: ["hf-files", submitted],
    queryFn: () => api.lmstudio.hfFiles(submitted),
    enabled: tab === "hf" && submitted.length > 0,
  });

  const downloadLmsMutation = useMutation({
    mutationFn: ({ modelId, quant }: { modelId: string; quant: string }) =>
      api.lmstudio.download(modelId, quant),
    onSuccess: (data) => {
      setMessage(data.message);
      setIsError(false);
      queryClient.invalidateQueries({ queryKey: ["lms-downloads"] });
    },
    onError: () => {
      setMessage(t("downloadStartFailed"));
      setIsError(true);
    },
  });

  const downloadHfMutation = useMutation({
    mutationFn: ({ repoId, filename }: { repoId: string; filename: string }) =>
      api.lmstudio.downloadHf(repoId, filename),
    onSuccess: (data) => {
      setMessage(data.message);
      setIsError(false);
    },
    onError: () => {
      setMessage(t("downloadStartFailed"));
      setIsError(true);
    },
  });

  const handleSearch = () => {
    const q = query.trim();
    if (!q) return;
    setMessage(null);
    setSubmitted(q);
  };

  const isLms = tab === "lms";
  const isMutating = downloadLmsMutation.isPending || downloadHfMutation.isPending;

  return (
    <div>
      {/* Tab Toggle */}
      <div
        className="flex gap-1 mb-4 p-1 rounded-lg"
        style={{ background: C.border }}
      >
        {(["lms", "hf"] as const).map((tabId) => (
          <button
            key={tabId}
            onClick={() => {
              setTab(tabId);
              setSubmitted("");
              setMessage(null);
            }}
            className="flex-1 text-xs py-2.5 sm:py-1.5 min-h-11 sm:min-h-0 rounded-md transition-colors cursor-pointer"
            style={{
              background: tab === tabId ? C.borderActive : "transparent",
              color: tab === tabId ? C.accent : C.textMuted,
              fontWeight: tab === tabId ? 500 : 400,
            }}
          >
            {tabId === "lms" ? "LM Studio" : "HuggingFace"}
          </button>
        ))}
      </div>

      {/* LM Studio Website Link */}
      {isLms && (
        <a
          href="https://lmstudio.ai/models"
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-1.5 text-xs mb-3 w-fit min-h-11 sm:min-h-0"
          style={{ color: C.textMuted }}
        >
          <ExternalLink size={11} />
          {t("openLmsSite")}
        </a>
      )}

      {/* Search field */}
      <div className="flex gap-2 mb-4">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSearch()}
          placeholder={isLms ? t("lmsPlaceholder") : t("hfPlaceholder")}
          aria-label={isLms ? t("searchLmsAria") : t("hfRepoAria")}
          className="flex-1 text-sm px-3 py-2.5 sm:py-2 min-h-11 sm:min-h-0 rounded-md outline-none"
          style={{
            background: C.border,
            border: `1px solid ${C.borderSubtle}`,
            color: C.textPrimary,
          }}
        />
        <button
          onClick={handleSearch}
          disabled={!query.trim()}
          className="text-xs px-3 py-2.5 sm:py-2 min-h-11 sm:min-h-0 rounded-md disabled:opacity-40 cursor-pointer disabled:cursor-not-allowed"
          style={{
            background: C.accentSubtle,
            border: `1px solid ${C.borderAccent}`,
            color: C.accent,
          }}
        >
          {t("search")}
        </button>
      </div>

      {/* Status Message */}
      {message && (
        <div
          className="text-xs mb-4 px-3 py-2 rounded-lg"
          style={{
            background: isError ? `${C.error}14` : C.accentSubtle,
            border: `1px solid ${isError ? `${C.error}33` : C.borderAccent}`,
            color: C.textSecondary,
          }}
        >
          {message}
        </div>
      )}

      {/* LM Studio results */}
      {isLms && submitted && (
        catalogLoading ? (
          <div className="text-xs text-center py-4" style={{ color: C.textMuted }}>
            {t("searching")}
          </div>
        ) : !catalogData?.models.length ? (
          <div className="text-xs text-center py-4" style={{ color: C.textMuted }}>
            {t("noResults", { query: submitted })}
          </div>
        ) : (
          <div className="flex flex-col gap-1.5">
            {catalogData.models.map((m) => {
              const baseName = m.model_id.split("/").pop()?.replace(/-gguf$/i, "").toLowerCase() ?? "";
              const installed = baseName.length > 0 && installedIds.some((id) => id.toLowerCase().includes(baseName));
              return (
                <div key={m.model_id}>
                  <ListRow
                    testId="lms-search-row"
                    tone={installed ? "ok" : "idle"}
                    name={m.name}
                    summary={[m.params, m.size_gb != null ? `${m.size_gb} GB` : null]
                      .filter(Boolean)
                      .join(" · ")}
                    chips={
                      installed ? (
                        <MetaChip tone="ok" icon={<Check size={10} />}>
                          {t("installed")}
                        </MetaChip>
                      ) : undefined
                    }
                    meta={
                      <MetaText className="tabular-nums shrink-0">
                        {[m.params, m.size_gb != null ? `${m.size_gb} GB` : null]
                          .filter(Boolean)
                          .join(" · ")}
                      </MetaText>
                    }
                    action={
                      installed ? undefined : (
                        <RowAction
                          icon={pickingModel === m.model_id ? <X size={10} /> : <ArrowDownToLine size={10} />}
                          onClick={() => setPickingModel(pickingModel === m.model_id ? null : m.model_id)}
                        >
                          {pickingModel === m.model_id ? t("cancel") : t("load")}
                        </RowAction>
                      )
                    }
                  />
                  {pickingModel === m.model_id && (
                    <QuantPicker
                      modelId={m.model_id}
                      onDownload={(quant) => {
                        downloadLmsMutation.mutate({ modelId: m.model_id, quant });
                        setPickingModel(null);
                      }}
                      isPending={downloadLmsMutation.isPending}
                    />
                  )}
                </div>
              );
            })}
          </div>
        )
      )}

      {/* HuggingFace results */}
      {!isLms && submitted && (
        hfLoading ? (
          <div className="text-xs text-center py-4" style={{ color: C.textMuted }}>
            {t("loadingRepo")}
          </div>
        ) : hfData?.error ? (
          <div
            className="text-xs px-3 py-2 rounded-lg"
            style={{
              background: `${C.error}14`,
              border: `1px solid ${C.error}26`,
              color: STATUS_TEXT.error,
            }}
          >
            {hfData.error}
          </div>
        ) : hfData ? (
          <div>
            <div className="text-xs mb-2 px-1" style={{ color: C.textMuted }}>
              {t("filesCount", { name: hfData.name, count: hfData.files.length })}
            </div>
            <div className="flex flex-col gap-1.5">
              {hfData.files.map((f) => (
                <ListRow
                  key={f.filename}
                  testId="hf-file-row"
                  tone="idle"
                  name={f.filename}
                  summary={`${f.size_gb} GB`}
                  meta={<MetaText className="tabular-nums shrink-0">{f.size_gb} GB</MetaText>}
                  action={
                    <RowAction
                      icon={downloadHfMutation.isPending
                        ? <Loader2 size={10} className="animate-spin" />
                        : <ArrowDownToLine size={10} />}
                      onClick={() =>
                        downloadHfMutation.mutate({ repoId: submitted, filename: f.filename })
                      }
                      disabled={isMutating}
                    >
                      {t("load")}
                    </RowAction>
                  }
                />
              ))}
            </div>
          </div>
        ) : null
      )}
    </div>
  );
}

// ── Models section (provider catalog · local recipes · download) ──────────────

type ModelsTab = "providers" | "local" | "download";

const MODELS_TAB_EVENT = "mc:models-tab";

/** Jump to the Models section and select a tab (used by the LM Studio pointer,
 *  and by SlotStage's "+ Modell" — exported so that leaf component can reuse
 *  the same section-scroll + tab-select mechanics instead of duplicating them). */
export function openModelsTab(tab: ModelsTab) {
  requestSectionOpen("models");
  window.dispatchEvent(new CustomEvent(MODELS_TAB_EVENT, { detail: tab }));
  requestAnimationFrame(() => {
    document.getElementById("models")?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

/** Jump to the Infrastructure section (hosts CRUD, KV reset schedule, vLLM
 *  container catalog, CLI tools) — shared target for both "Hosts & Zeitpläne"
 *  and "CLI-Tools" footer links, mirroring openModelsTab's mechanics. */
function openInfraSection() {
  requestSectionOpen("infrastructure");
  requestAnimationFrame(() => {
    document.getElementById("infrastructure")?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

function ModelsSection() {
  const t = useTranslations("runtimes.models");
  const [tab, setTab] = useState<ModelsTab>("providers");

  useEffect(() => {
    const onTab = (e: Event) => setTab((e as CustomEvent<ModelsTab>).detail);
    window.addEventListener(MODELS_TAB_EVENT, onTab);
    return () => window.removeEventListener(MODELS_TAB_EVENT, onTab);
  }, []);

  // Same query keys the child components use, so TanStack serves these from
  // cache — the counts cost no extra request.
  const { data: catalog } = useQuery({
    queryKey: ["model-catalog"],
    queryFn: () => api.modelCatalog.list(),
  });
  const { data: local } = useQuery({
    queryKey: ["local-registry", false],
    queryFn: () => api.localRegistry.list({ enabled: true }),
    retry: false,
  });

  const providerCount = catalog?.providers?.length ?? 0;
  const recipeCount = local?.recipes?.length ?? 0;

  const tabs: { id: ModelsTab; label: string; count?: number }[] = [
    { id: "providers", label: t("tabProviders"), count: providerCount },
    { id: "local", label: t("tabLocal"), count: recipeCount },
    { id: "download", label: t("tabDownload") },
  ];

  return (
    <Section
      id="models"
      title={t("title")}
      hint={t("subtitle")}
      defaultOpen={false}
      // No count on the header: providers and recipes are different things and
      // their sum ("11") would read as a model count. Each tab counts itself.
    >
      <div
        role="tablist"
        aria-label={t("title")}
        className="flex items-center gap-1 mb-3 flex-wrap"
      >
        {tabs.map((item) => {
          const active = tab === item.id;
          return (
            <button
              key={item.id}
              role="tab"
              aria-selected={active}
              onClick={() => setTab(item.id)}
              data-testid={`models-tab-${item.id}`}
              className="inline-flex items-center gap-1.5 rounded-md px-3 py-2 min-h-11 sm:min-h-0 sm:px-2.5 sm:py-1.5 text-xs cursor-pointer transition-colors"
              style={{
                background: active ? C.accentSubtle : "transparent",
                border: `1px solid ${active ? C.borderAccent : C.borderSubtle}`,
                color: active ? C.textPrimary : C.textMuted,
              }}
            >
              {item.label}
              {item.count != null && (
                <span className="label-sys tabular-nums" style={{ color: C.textDim }}>
                  {item.count}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {tab === "providers" && <ModelCatalogSection embedded />}
      {tab === "local" && <LocalModelBrowser embedded />}
      {tab === "download" && (
        <>
          <ActiveDownloads />
          <ModelCatalog />
        </>
      )}
    </Section>
  );
}

// ── KV Reset Schedule Toggle ──────────────────────────────────────────────────

function KvResetScheduleToggle() {
  const t = useTranslations("runtimes.kv");
  const [open, setOpen] = useState(false);
  const [resetMsg, setResetMsg] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const { data: schedules } = useQuery({
    queryKey: ["runtime-schedules", "lmstudio"],
    queryFn: () => api.runtimes.schedules.list("lmstudio"),
    refetchInterval: 30_000,
  });

  const kvResetMutation = useMutation({
    mutationFn: () => api.lmstudio.kvReset(),
    onSuccess: (data) => {
      setResetMsg(data.message);
      queryClient.invalidateQueries({ queryKey: ["lmstudio-models"] });
    },
    onError: () => setResetMsg(t("failed")),
  });

  const activeSchedule = schedules?.find((s) => s.action === "kv_reset" && s.enabled);

  return (
    <div>
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1.5 text-xs px-2.5 py-2 sm:py-1.5 min-h-11 sm:min-h-0 rounded-md transition-all cursor-pointer"
        style={{
          background: open ? `${C.warning}1A` : C.borderSubtle,
          border: open ? `1px solid ${C.warning}4D` : `1px solid ${C.borderSubtle}`,
          color: open ? C.warning : C.textMuted,
        }}
        title={t("scheduleTitle")}
      >
        <Clock size={11} />
        {t("toggle")}
        {activeSchedule && (
          <MetaChip tone="ok" className="tabular-nums">
            {activeSchedule.time_of_day}
          </MetaChip>
        )}
      </button>

      {open && (
        <motion.div
          initial={{ opacity: 0, y: -4 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.15 }}
          className="mt-2 rounded-xl overflow-hidden"
          style={{
            border: `1px solid ${C.warning}33`,
            background: `${C.warning}08`,
          }}
        >
          <div
            className="flex items-center justify-between gap-3 px-4 py-2.5"
            style={{ borderBottom: `1px solid ${C.warning}26` }}
          >
            <div className="flex items-center gap-2 min-w-0">
              <span className="text-xs font-medium" style={{ color: C.warning }}>{t("scheduleTitle")}</span>
              <span className="text-xs" style={{ color: C.textMuted }}>
                {t("scheduleHint")}
              </span>
            </div>
            <button
              onClick={() => { setResetMsg(null); kvResetMutation.mutate(); }}
              disabled={kvResetMutation.isPending}
              className="shrink-0 flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed transition-opacity"
              style={{
                background: `${C.warning}1A`,
                border: `1px solid ${C.warning}40`,
                color: C.warning,
              }}
            >
              {kvResetMutation.isPending ? (
                <Loader2 size={11} className="animate-spin" />
              ) : <EntityIcon value="⚡" size={11} />}
              {t("runNow")}
            </button>
          </div>
          {resetMsg && (
            <div
              className="mx-4 mt-3 text-xs px-3 py-2 rounded-lg"
              style={{
                background: kvResetMutation.isError ? `${C.error}14` : `${C.online}14`,
                border: `1px solid ${kvResetMutation.isError ? `${C.error}33` : `${C.online}33`}`,
                color: C.textSecondary,
              }}
            >
              {resetMsg}
            </div>
          )}
          <RuntimeScheduleTab runtimeId="lmstudio" runtimeType="lmstudio" />
        </motion.div>
      )}
    </div>
  );
}

// ── Sleeping host line ────────────────────────────────────────────────────────
// Power-managed hosts (e.g. PORSCHE) with no serving runtime render as a single
// quiet row instead of a full stage — a stage implies an active GPU slot to
// inspect, an asleep box is just one action away ("Wecken").

function SleepingHostLine({ group }: { group: HostGroup }) {
  const t = useTranslations("runtimes");
  const tSlot = useTranslations("runtimes.slotPage");
  const queryClient = useQueryClient();
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  const runtime = group.runtimes.find((rt) => rt.power_managed === true);

  const wakeMutation = useMutation({
    mutationFn: () => api.runtimes.wake(runtime!.id),
    onSuccess: (data) => {
      setActionMsg(data.message);
      queryClient.invalidateQueries({ queryKey: ["runtimes"] });
    },
    onError: () => setActionMsg(t("wakeFailed")),
  });

  // Nothing to wake — a power-managed host with no bound runtime yet is
  // reachable via Infrastructure → Hosts, not worth a row here.
  if (!runtime) return null;

  const isAsleep = runtime.container_status === "asleep";
  const statusText = isAsleep ? t("sleeping") : t("awakeNoModel");

  return (
    <div className="flex flex-col gap-1.5">
      <div
        className="flex items-center justify-between gap-3 px-3.5 py-2.5 rounded-lg"
        style={{ background: C.bgSurface, border: `1px solid ${C.borderSubtle}` }}
      >
        <span className="text-xs" style={{ color: C.textMuted }}>
          {tSlot("sleepingLine", { host: group.host.display_name, status: statusText })}
        </span>
        <button
          onClick={() => { setActionMsg(null); wakeMutation.mutate(); }}
          disabled={wakeMutation.isPending}
          className="flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-md cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed transition-opacity shrink-0"
          style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.accent }}
        >
          {wakeMutation.isPending ? <Loader2 size={11} className="animate-spin" /> : <Power size={11} />}
          {t("wake")}
        </button>
      </div>
      {actionMsg && (
        <div
          className="text-[11px] px-2.5 py-1.5 rounded-md"
          style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.textSecondary }}
        >
          {actionMsg}
        </div>
      )}
    </div>
  );
}

// ── Unassigned quiet row ───────────────────────────────────────────────────────
// Hostless, non-cloud runtimes (e.g. hermes) — never silently hidden, but
// demoted to a one-line hint instead of a card.

function UnassignedRow({ runtime, onOpen }: { runtime: Runtime; onOpen: (rt: Runtime) => void }) {
  const tSlot = useTranslations("runtimes.slotPage");
  return (
    <button
      type="button"
      data-testid={`unassigned-row-${runtime.slug ?? runtime.id}`}
      onClick={() => onOpen(runtime)}
      className="flex items-center justify-between gap-3 px-3.5 py-2.5 rounded-lg text-left cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
      style={{ background: C.bgSurface, border: `1px solid ${C.borderSubtle}`, opacity: 0.7 }}
    >
      <span className="text-xs truncate" style={{ color: C.textMuted }}>{runtime.display_name}</span>
      <span className="text-xs shrink-0" style={{ color: C.textDim }}>{tSlot("unassignedHint")}</span>
    </button>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function RuntimesPage() {
  const t = useTranslations("runtimes");
  const tSlot = useTranslations("runtimes.slotPage");
  const tInfra = useTranslations("runtimes.infrastructure");
  const [addOpen, setAddOpen] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["runtimes"],
    queryFn: () => api.runtimes.list(),
    refetchInterval: 15_000,
  });

  const { data: hostsData } = useQuery<Host[]>({
    queryKey: ["hosts"],
    queryFn: api.hosts.list,
  });

  const { data: lmsData } = useQuery({
    queryKey: ["lmstudio-models"],
    queryFn: () => api.lmstudio.list(),
    refetchInterval: 15_000,
  });

  const { data: liveData } = useQuery({
    queryKey: ["runtimes", "live-status"],
    queryFn: () => api.runtimes.liveStatus(),
    refetchInterval: 30_000,
  });

  const allRuntimes = data?.runtimes ?? [];
  const hosts = hostsData ?? [];
  const live: Record<string, RuntimeLiveStatus> | undefined = liveData?.live;

  const lmsSizeMap = new Map((lmsData?.models ?? []).map((m) => [m.id, m.size_gb]));
  const getSizeGb = (rt: Runtime) => lmsSizeMap.get(rt.lms_identifier ?? "") ?? undefined;

  const groups = groupRuntimes(allRuntimes, hosts);

  // Power-managed hosts with nothing currently serving render as a sleeping
  // line instead of a stage (mockup M1). Everything else that's enabled and
  // has at least one lifecycle-capable runtime gets the full stage.
  const stageGroups = groups.hosts.filter(
    (g) =>
      g.host.enabled &&
      g.runtimes.some((rt) => panelCapabilities(rt).lifecycle) &&
      !(g.host.power_managed && pickServing(g, live) === null)
  );
  const sleepingGroups = groups.hosts.filter(
    (g) => g.host.enabled && g.host.power_managed && pickServing(g, live) === null
  );

  const selectedRuntime = allRuntimes.find((rt) => rt.id === selectedId) ?? null;
  const selectedLive = selectedRuntime ? live?.[selectedRuntime.slug ?? selectedRuntime.id] : undefined;
  const openPanel = (rt: Runtime) => setSelectedId(rt.id);

  const isEmpty = !isLoading && !error && allRuntimes.length === 0;

  return (
    <AppShell>
      <div className="p-6 max-w-4xl mx-auto">
        {/* Header */}
        <div className="flex items-center justify-between mb-6">
          <div>
            <div className="label-sys mb-2">System · Runtimes</div>
            <h1
              className="display text-xl font-semibold"
              style={{ color: C.textPrimary }}
            >
              {t("title")}
            </h1>
            <p
              className="text-[13px] mt-0.5"
              style={{ color: C.textSecondary }}
            >
              {tSlot("subtitle")}
            </p>
          </div>

          <button
            onClick={() => setAddOpen(true)}
            className="flex items-center gap-1.5 text-xs px-3 py-2 sm:py-1.5 min-h-11 sm:min-h-0 rounded-md transition-all cursor-pointer"
            style={{
              color: C.accent,
              border: `1px solid ${C.borderAccent}`,
              background: C.accentSubtle,
            }}
          >
            <Plus size={11} />
            {t("addRuntime")}
          </button>
        </div>

        {isLoading && (
          <div className="flex items-center gap-2 py-2" style={{ color: C.textMuted }}>
            <Loader2 size={13} className="animate-spin" />
            <span className="text-xs">{t("loading")}</span>
          </div>
        )}

        {error && (
          <div
            className="flex items-center gap-2 text-xs px-2.5 py-2 rounded-md mb-6"
            style={{ color: STATUS_TEXT.error, background: "transparent", border: `1px solid ${C.error}40` }}
          >
            <AlertCircle size={13} />
            {t("loadError")}
          </div>
        )}

        {isEmpty && (
          <div className="flex flex-col items-center gap-3 py-16 text-center">
            <span className="text-sm" style={{ color: C.textMuted }}>{tSlot("emptyTitle")}</span>
            <span className="text-xs max-w-xs" style={{ color: C.textDim }}>{tSlot("emptyHint")}</span>
            <button
              onClick={() => setAddOpen(true)}
              className="mt-1 flex items-center gap-1.5 text-xs px-3 py-2 sm:py-1.5 min-h-11 sm:min-h-0 rounded-md transition-all cursor-pointer"
              style={{ color: C.accent, border: `1px solid ${C.borderAccent}`, background: C.accentSubtle }}
            >
              <Plus size={11} />
              {t("addRuntime")}
            </button>
          </div>
        )}

        {!isLoading && !error && !isEmpty && (
          <div className="flex flex-col gap-6">
            {stageGroups.map((g) => (
              <SlotStage key={g.host.id} group={g} live={live} sizeGb={getSizeGb} onOpen={openPanel} />
            ))}

            {sleepingGroups.length > 0 && (
              <div className="flex flex-col gap-2">
                {sleepingGroups.map((g) => (
                  <SleepingHostLine key={g.host.id} group={g} />
                ))}
              </div>
            )}

            <CloudUsage runtimes={groups.cloud} onOpen={openPanel} />

            {groups.unassigned.length > 0 && (
              <section>
                <div className="flex items-center gap-2.5 mb-3">
                  <span
                    className="text-[10px] font-medium uppercase shrink-0"
                    style={{ color: C.textMuted, letterSpacing: "0.08em" }}
                  >
                    {tSlot("unassignedTitle")}
                  </span>
                  <div className="flex-1 h-px" style={{ background: C.borderSubtle }} />
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  {groups.unassigned.map((rt) => (
                    <UnassignedRow key={rt.id} runtime={rt} onOpen={openPanel} />
                  ))}
                </div>
              </section>
            )}
          </div>
        )}

        {/* Quiet footer — three text links replacing the old always-open sections */}
        <div
          className="flex items-center gap-4 mt-10 pt-4"
          style={{ borderTop: `1px solid ${C.borderSubtle}` }}
        >
          <button
            type="button"
            onClick={() => openModelsTab("download")}
            data-testid="footer-models"
            className="text-xs cursor-pointer transition-colors hover:underline"
            style={{ color: C.textMuted }}
          >
            {tSlot("footerModels")}
          </button>
          <button
            type="button"
            onClick={openInfraSection}
            data-testid="footer-infra"
            className="text-xs cursor-pointer transition-colors hover:underline"
            style={{ color: C.textMuted }}
          >
            {tSlot("footerInfra")}
          </button>
          <button
            type="button"
            onClick={openInfraSection}
            data-testid="footer-cli"
            className="text-xs cursor-pointer transition-colors hover:underline"
            style={{ color: C.textMuted }}
          >
            {tSlot("footerCli")}
          </button>
        </div>

        <ModelsSection />

        <Section id="infrastructure" title={tInfra("title")} hint={tInfra("subtitle")} defaultOpen={false}>
          <div className="flex flex-col gap-5">
            <HostsSection embedded />
            <KvResetScheduleToggle />
            <VllmContainerCatalog />
            <CliToolsSection embedded />
          </div>
        </Section>
      </div>

      <RuntimeDetailPanel
        runtime={selectedRuntime}
        live={selectedLive}
        open={selectedId != null}
        onClose={() => setSelectedId(null)}
      />
      <AddRuntimeModal open={addOpen} onClose={() => setAddOpen(false)} />
    </AppShell>
  );
}

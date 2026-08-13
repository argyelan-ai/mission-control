"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { motion } from "framer-motion";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDownToLine,
  ExternalLink,
  Play,
  Power,
  Square,
  RotateCcw,
  RefreshCw,
  Loader2,
  AlertCircle,
  CheckCircle2,
  Clock,
  WifiOff,
  Download,
  ChevronDown,
  ChevronUp,
  Settings2,
  Plus,
  X,
  Pencil,
  Check,
  type LucideIcon,
} from "lucide-react";
import { api } from "@/lib/api";
import type { Runtime, RuntimeState, RuntimeLiveStatus, LMStudioModel, LMSCatalogModel, HFRepoInfo, LMStudioModelsResponse, LMSActiveDownload, VllmContainer } from "@/lib/types";
import AppShell from "@/components/layout/AppShell";
import { cn, fmtCtx } from "@/lib/utils";
import { RuntimeScheduleTab } from "./RuntimeScheduleTab";
import { VllmContainerCatalog } from "./VllmContainerCatalog";
import { AddRuntimeModal } from "./AddRuntimeModal";
import { HostMetricsBar, HostsSection } from "./HostsSection";
import { AutostartToggle } from "./AutostartToggle";
import { CliToolsSection } from "@/components/shared/CliToolsSection";
import { ModelCatalogSection } from "@/components/shared/ModelCatalogSection";
import { LocalModelBrowser } from "@/components/shared/LocalModelBrowser";
import { BindAgentModal } from "@/components/shared/BindAgentModal";
import { SparkRecipeSwitcher } from "@/components/shared/SparkRecipeSwitcher";
import Link from "next/link";
import { Plug } from "lucide-react";
import { C, STATUS, STATUS_TEXT } from "@/lib/colors";
import { EntityIcon } from "@/components/shared/EntityIcon";
import { OverflowMenu } from "@/components/shared/OverflowMenu";
import { Section, SectionNav, requestSectionOpen } from "@/components/shared/Section";
import { ListRow, MetaChip, MetaText, RowAction, type Tone } from "@/components/shared/ListRow";

// ── State Configuration ───────────────────────────────────────────────────────

// labelKey pattern (docs/i18n.md): resolved via t() at the render site.
const STATE_CONFIG: Record<
  RuntimeState,
  { labelKey: string; color: string; dot: string; icon: LucideIcon }
> = {
  ready: {
    labelKey: "states.ready",
    color: C.online,
    dot: C.online,
    icon: CheckCircle2,
  },
  warming: {
    labelKey: "states.warming",
    color: C.warning,
    dot: C.warning,
    icon: Clock,
  },
  starting: {
    labelKey: "states.starting",
    color: C.info,
    dot: C.info,
    icon: Loader2,
  },
  stopped: {
    labelKey: "states.stopped",
    color: C.textMuted,
    dot: STATUS.offline,
    icon: Square,
  },
  failed: {
    labelKey: "states.failed",
    color: C.error,
    dot: C.error,
    icon: AlertCircle,
  },
  unknown: {
    labelKey: "states.unknown",
    color: C.textMuted,
    dot: STATUS.offline,
    icon: WifiOff,
  },
};

const STATE_TONE: Record<RuntimeState, Tone> = {
  ready: "ok",
  warming: "warn",
  starting: "warn",
  stopped: "idle",
  failed: "error",
  unknown: "idle",
};

/** The one note style under a row (action feedback). */
function RowNote({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="text-[11px] px-2.5 py-1.5 rounded-md"
      style={{
        background: C.accentSubtle,
        border: `1px solid ${C.borderAccent}`,
        color: C.textSecondary,
      }}
    >
      {children}
    </div>
  );
}

// ── Action Button ─────────────────────────────────────────────────────────────

function ActionButton({
  icon: Icon,
  label,
  disabled,
  onClick,
  loading,
  variant,
}: {
  icon: LucideIcon;
  label: string;
  disabled: boolean;
  onClick: () => void;
  loading: boolean;
  variant: "success" | "danger" | "default";
}) {
  // Outlined, not filled. A filled error-red Stop button on every running
  // runtime made red the loudest colour on a page where nothing is wrong —
  // and red already means "failed" elsewhere. The hue still carries the
  // action; the fill only appears on hover.
  const colors = {
    success: { border: `${C.online}40`, text: C.online, hover: `${C.online}1A` },
    danger:  { border: `${C.error}33`, text: STATUS_TEXT.error, hover: `${C.error}1A` },
    default: { border: C.borderActive, text: C.textMuted, hover: C.bgHover },
  };
  const c = colors[variant];

  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={label}
      aria-label={label}
      // The hit area is 44px on a thumb, but the drawn control stays 28px:
      // stretching the border to the full target turned Stop into a big empty
      // red box. Touch target and visual weight are two different things.
      className="flex items-center justify-center w-11 h-11 sm:w-7 sm:h-7 min-w-11 sm:min-w-[28px] cursor-pointer disabled:cursor-not-allowed"
    >
      <span
        aria-hidden
        className="action-btn flex items-center justify-center w-7 h-7 rounded-md transition-colors"
        style={{
          background: "transparent",
          // Disabled used to render at rgba(168,168,168,0.16) — about 1.1:1, so
          // the operator could not tell "disabled" from "not there". textDim
          // clears the 3:1 floor for UI components (WCAG 1.4.11).
          border: `1px solid ${disabled ? C.borderSubtle : c.border}`,
          color: disabled ? C.textDim : c.text,
          ["--action-hover" as string]: c.hover,
        }}
      >
        {loading ? <Loader2 size={12} className="animate-spin" /> : <Icon size={12} />}
      </span>
    </button>
  );
}

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

// ── Context Presets ───────────────────────────────────────────────────────────

const CTX_PRESETS = [4096, 8192, 16384, 32768, 65536, 131072, 200000, 262144];

const CTX_STORAGE_KEY = (modelId: string) => `lms-ctx-${modelId}`;

function loadStoredCtx(modelId: string): number | null {
  try {
    const v = localStorage.getItem(CTX_STORAGE_KEY(modelId));
    return v ? parseInt(v, 10) : null;
  } catch { return null; }
}

function saveStoredCtx(modelId: string, ctx: number | null) {
  try {
    if (ctx === null) localStorage.removeItem(CTX_STORAGE_KEY(modelId));
    else localStorage.setItem(CTX_STORAGE_KEY(modelId), String(ctx));
  } catch {}
}

// ── Context Settings Panel ────────────────────────────────────────────────────

function ContextSettingsPanel({
  modelId,
  initialCtx,
  onClose,
}: {
  modelId: string;
  initialCtx: number | null;
  onClose: () => void;
}) {
  const t = useTranslations("runtimes.ctx");
  // null = "Standard" (no override — LM Studio global default)
  const [selected, setSelected] = useState<number | null>(initialCtx);
  const [customInput, setCustomInput] = useState("");
  const [customError, setCustomError] = useState(false);

  const handleSave = () => {
    saveStoredCtx(modelId, selected);
    onClose();
  };

  const handleCustomInput = (v: string) => {
    setCustomInput(v);
    const n = parseInt(v.replace(/\D/g, ""), 10);
    if (!isNaN(n) && n >= 512 && n <= 1048576) {
      setSelected(n);
      setCustomError(false);
    } else {
      setCustomError(true);
    }
  };

  const isStandard = selected === null;
  const isPreset = selected !== null && CTX_PRESETS.includes(selected);

  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: "auto" }}
      exit={{ opacity: 0, height: 0 }}
      transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
      style={{ overflow: "hidden" }}
    >
      <div
        className="mx-3 mb-2.5 rounded-lg p-3"
        style={{
          background: C.borderSubtle,
          border: `1px solid ${C.border}`,
        }}
      >
        <div className="flex items-center justify-between mb-2.5">
          <span className="text-xs font-medium" style={{ color: C.textMuted, letterSpacing: "0.04em" }}>
            {t("title")}
          </span>
          <span className="text-xs font-mono tabular-nums" style={{ color: C.textPrimary }}>
            {isStandard ? t("standardValue") : t("tokensValue", { n: selected!.toLocaleString() })}
          </span>
        </div>

        {/* Preset pills — Standard + numeric presets */}
        <div className="flex gap-1.5 flex-wrap mb-3">
          <button
            onClick={() => setSelected(null)}
            className="text-xs px-2.5 py-1 rounded-md cursor-pointer transition-all"
            style={{
              background: isStandard ? C.borderActive : C.borderSubtle,
              border: `1px solid ${isStandard ? C.borderActive : C.border}`,
              color: isStandard ? C.textPrimary : C.textMuted,
              fontWeight: isStandard ? 600 : 400,
            }}
          >
            {t("standard")}
          </button>
          {CTX_PRESETS.map((preset) => {
            const active = selected === preset;
            return (
              <button
                key={preset}
                onClick={() => setSelected(preset)}
                className="text-xs px-2.5 py-1 rounded-md cursor-pointer transition-all"
                style={{
                  background: active ? C.accentSubtle : C.borderSubtle,
                  border: `1px solid ${active ? C.borderAccent : C.border}`,
                  color: active ? C.accent : C.textMuted,
                  fontWeight: active ? 600 : 400,
                }}
              >
                {fmtCtx(preset)}
              </button>
            );
          })}
        </div>

        {/* Slider — only active when not Standard */}
        <div className="mb-3">
          <input
            type="range"
            min={0}
            max={CTX_PRESETS.length - 1}
            value={selected !== null && CTX_PRESETS.indexOf(selected) >= 0 ? CTX_PRESETS.indexOf(selected) : 3}
            onChange={(e) => {
              const v = CTX_PRESETS[parseInt(e.target.value)];
              setSelected(v);
              setCustomInput(String(v));
              setCustomError(false);
            }}
            disabled={isStandard}
            aria-label={t("presetAria")}
            className="w-full cursor-pointer disabled:opacity-30"
            style={{ accentColor: C.accent, height: "2px" }}
          />
          <div className="flex justify-between mt-1">
            <span style={{ color: C.borderActive, fontSize: "10px" }}>4k</span>
            <span style={{ color: C.borderActive, fontSize: "10px" }}>262k</span>
          </div>
        </div>

        {/* Custom Input */}
        <div className="mb-3">
          <div className="flex items-center gap-2">
            <span style={{ color: C.textDim, fontSize: "10px", whiteSpace: "nowrap" }}>
              {t("custom")}
            </span>
            <input
              type="text"
              inputMode="numeric"
              placeholder={t("customPlaceholder")}
              value={customInput}
              disabled={isStandard}
              aria-label={t("customAria")}
              onChange={(e) => handleCustomInput(e.target.value)}
              className="flex-1 text-xs font-mono px-2 py-1 rounded disabled:opacity-30"
              style={{
                background: C.borderSubtle,
                border: `1px solid ${customError ? C.error : C.border}`,
                color: customError ? STATUS_TEXT.error : C.textPrimary,
                minWidth: 0,
              }}
            />
            <span style={{ color: C.textDim, fontSize: "10px" }}>{t("tokensUnit")}</span>
          </div>
          {customError && (
            <span style={{ color: STATUS_TEXT.error, fontSize: "10px" }}>512 – 1'048'576</span>
          )}
        </div>

        {/* Hint + Save */}
        <div className="flex items-center justify-between gap-2">
          <span style={{ color: C.textDim, fontSize: "10px" }}>
            {isStandard ? t("usesGlobal") : t("appliedNextLoad")}
          </span>
          <button
            onClick={handleSave}
            disabled={customError}
            className="text-xs px-3 py-1 rounded-md cursor-pointer transition-all disabled:opacity-40 disabled:cursor-not-allowed"
            style={{
              background: C.accentSubtle,
              border: `1px solid ${C.borderAccent}`,
              color: C.accent,
            }}
          >
            {t("save")}
          </button>
        </div>
      </div>
    </motion.div>
  );
}

// ── LM Studio Model Row ───────────────────────────────────────────────────────

function LMStudioModelCard({ model }: { model: LMStudioModel }) {
  const t = useTranslations("runtimes");
  const queryClient = useQueryClient();
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const storedCtx = loadStoredCtx(model.id);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["lmstudio-models"] });
  };

  const loadMutation = useMutation({
    mutationFn: () => api.lmstudio.load(model.id, storedCtx ?? undefined),
    onSuccess: (data) => { setActionMsg(data.message); invalidate(); },
    onError: () => setActionMsg(t("loadFailed")),
  });

  const unloadMutation = useMutation({
    mutationFn: () => api.lmstudio.unload(model.id),
    onSuccess: (data) => { setActionMsg(data.message); invalidate(); },
    onError: () => setActionMsg(t("unloadFailed")),
  });

  const isMutating = loadMutation.isPending || unloadMutation.isPending;
  const accentColor = model.is_loaded ? C.online : C.borderActive;

  return (
    <div className="flex flex-col gap-1.5">
      <ListRow
        testId="lms-model-row"
        dataAttrs={{ "data-model": model.id }}
        tone={model.is_loaded ? "ok" : "idle"}
        name={model.display_name}
        summary={[
          model.is_loaded ? t("states.ready") : t("states.stopped"),
          "LM Studio",
          model.size_gb > 0 ? `${model.size_gb.toFixed(1)} GB` : null,
        ]
          .filter(Boolean)
          .join(" · ")}
        chips={
          <>
            {model.is_embedding && <MetaChip tone="idle">EMBED</MetaChip>}
            <MetaChip tone="idle">LM Studio</MetaChip>
            {storedCtx && (
              <MetaChip tone="idle" className="tabular-nums">
                {fmtCtx(storedCtx)} ctx
              </MetaChip>
            )}
          </>
        }
        meta={
          model.size_gb > 0 ? (
            <MetaText className="tabular-nums shrink-0">{model.size_gb.toFixed(1)} GB</MetaText>
          ) : undefined
        }
        action={
          model.is_loaded ? (
            <ActionButton
              icon={Square}
              label={t("unload")}
              disabled={isMutating}
              onClick={() => unloadMutation.mutate()}
              loading={unloadMutation.isPending}
              variant="danger"
            />
          ) : (
            <ActionButton
              icon={Play}
              label={t("load")}
              disabled={isMutating}
              onClick={() => loadMutation.mutate()}
              loading={loadMutation.isPending}
              variant="success"
            />
          )
        }
        overflow={
          !model.is_embedding ? (
            <OverflowMenu
              label={t("moreActions", { name: model.display_name })}
              testId={`lms-more-${model.id}`}
              actions={[
                {
                  id: "ctx",
                  label: t("ctxSettings"),
                  icon: Settings2,
                  onClick: () => setSettingsOpen((o) => !o),
                },
              ]}
            />
          ) : undefined
        }
      />

      {settingsOpen && !model.is_embedding && (
        <ContextSettingsPanel
          modelId={model.id}
          initialCtx={storedCtx}
          onClose={() => setSettingsOpen(false)}
        />
      )}

      {actionMsg && <RowNote>{actionMsg}</RowNote>}
    </div>
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

// ── Model Catalog ─────────────────────────────────────────────────────────────

function ModelCatalog() {
  const t = useTranslations("runtimes.catalog");
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
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

  // The download source is a choice, not a state. Green/orange here meant
  // nothing — the selected tab carries the accent, the other stays neutral.

  // Inside the Models tab strip this panel is already the selected surface — a
  // second "Download model" collapse head on top of it was one disclosure
  // nested in another, each with its own chrome.
  return (
    <div>
      {(
        <div>
          {/* Tab Toggle */}
          <div
            className="flex gap-1 mb-4 p-1 rounded-lg"
            style={{ background: C.border }}
          >
            {(["lms", "hf"] as const).map((t) => (
              <button
                key={t}
                onClick={() => {
                  setTab(t);
                  setSubmitted("");
                  setMessage(null);
                }}
                className="flex-1 text-xs py-2.5 sm:py-1.5 min-h-11 sm:min-h-0 rounded-md transition-colors cursor-pointer"
                style={{
                  background: tab === t ? C.borderActive : "transparent",
                  color: tab === t ? C.accent : C.textMuted,
                  fontWeight: tab === t ? 500 : 400,
                }}
              >
                {t === "lms" ? "LM Studio" : "HuggingFace"}
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
      )}
    </div>
  );
}

// ── Runtime Row ───────────────────────────────────────────────────────────────

// ── Bound Agents (Phase 15 T3.3) ────────────────────────────────────────
// Shows the agents currently using this runtime + a "Bind Agent" button
// that opens BindAgentModal. Only visible for runtimes that have a slug
// (DB-managed); legacy JSON runtimes are skipped.

function BoundAgents({ runtime }: { runtime: Runtime }) {
  const t = useTranslations("runtimes");
  const [bindOpen, setBindOpen] = useState(false);
  const slug = runtime.slug ?? runtime.id;
  const queryClient = useQueryClient();

  const { data, isLoading } = useQuery({
    queryKey: ["runtimes", slug, "agents"],
    queryFn: () => api.runtimes.db.agents(slug),
    enabled: !!slug,
    staleTime: 15_000,
    retry: false,
  });

  const bound = data?.agents ?? [];
  const anyPending = bound.some((a) => a.pending_runtime_sync);

  const syncNowMutation = useMutation({
    mutationFn: () => api.runtimes.db.syncAgents(slug),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["runtimes", slug, "agents"] });
      queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  });

  // Inline chips in the meta line, not a bordered footer bar. The old footer
  // cost a full row plus a divider on every card, and stacked five identical
  // "Bind agent" buttons down the page.
  return (
    <>
      <span className="inline-flex items-center gap-1.5 flex-wrap">
        <span className="label-sys" style={{ color: C.textDim }}>
          {t("agentsLabel")}
        </span>
        {isLoading && <Loader2 size={11} className="animate-spin" style={{ color: C.textMuted }} />}
        {!isLoading && bound.length === 0 && (
          <span className="text-xs" style={{ color: C.textMuted }}>
            {t("noneUnbound")}
          </span>
        )}
        {bound.map((a) => (
          <span key={a.id} className="inline-flex items-center gap-1">
            <Link
              href={`/agents/${a.id}`}
              className="inline-flex items-center gap-1 label-sys rounded-sm px-1.5 py-0.5 leading-none min-h-11 sm:min-h-6 hover:bg-[var(--color-bg-hover)] transition-colors cursor-pointer"
              style={{
                backgroundColor: C.accentSubtle,
                color: C.textSecondary,
                border: `1px solid ${C.borderAccent}`,
              }}
              title={`${a.name} · ${a.agent_runtime}`}
            >
              <EntityIcon value="🤖" size={13} className="inline-block align-[-2px] mr-1" />{a.name}
            </Link>
            {a.pending_runtime_sync && (
              <MetaChip tone="warn" title={t("pendingSyncTitle")}>
                {t("pendingSync")}
              </MetaChip>
            )}
          </span>
        ))}
        <button
          onClick={() => setBindOpen(true)}
          title={t("bindAgent")}
          className="inline-flex items-center gap-1 label-sys rounded-sm px-1.5 py-0.5 leading-none min-h-11 sm:min-h-6 cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
          style={{
            color: C.textSecondary,
            border: `1px dashed ${C.borderActive}`,
          }}
        >
          <Plug size={10} />
          {t("bindAgent")}
        </button>
        {anyPending && (
          <button
            onClick={() => syncNowMutation.mutate()}
            className="underline cursor-pointer text-xs"
            style={{ color: STATUS_TEXT.warning }}
            title={t("syncNowTitle")}
          >
            {t("syncNow")}
          </button>
        )}
      </span>

      <BindAgentModal
        open={bindOpen}
        onClose={() => setBindOpen(false)}
        runtime={runtime}
      />
    </>
  );
}

// Inline model_identifier editor for non-probeable runtimes (cloud/Anthropic).
// These have no watcher-driven live model, so their static DB value is the only
// source of truth — and needs a manual edit path (e.g. Opus 4.7 → 4.8). Probeable
// runtimes edit their model via Re-probe instead, so this is not rendered there.
function RuntimeModelEditor({
  runtime,
  onMessage,
}: {
  runtime: Runtime;
  onMessage?: (msg: string) => void;
}) {
  const t = useTranslations("runtimes.modelEditor");
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(runtime.model_identifier ?? "");

  const mutation = useMutation({
    mutationFn: (model: string) =>
      api.runtimes.db.update(runtime.slug ?? runtime.id, { model_identifier: model }),
    onSuccess: (data) => {
      setEditing(false);
      queryClient.invalidateQueries({ queryKey: ["runtimes"] });
      queryClient.invalidateQueries({ queryKey: ["agents"] });
      onMessage?.(t("modelSet", { model: data.model_identifier ?? "—" }));
    },
    onError: () => onMessage?.(t("updateFailed")),
  });

  const save = () => {
    const trimmed = value.trim();
    if (!trimmed || trimmed === (runtime.model_identifier ?? "")) {
      setEditing(false);
      return;
    }
    mutation.mutate(trimmed);
  };

  const cancel = () => {
    setValue(runtime.model_identifier ?? "");
    setEditing(false);
  };

  // 20px hit areas missed WCAG 2.2 SC 2.5.8 (24x24); comfortable on a thumb.
  const iconBtnClass =
    "flex items-center justify-center w-11 h-11 sm:w-6 sm:h-6 min-w-11 sm:min-w-6 rounded-md cursor-pointer";
  const iconBtn = (color: string) => ({
    background: "transparent" as const,
    border: "1px solid transparent",
    color,
  });

  if (editing) {
    return (
      <div className="flex items-center gap-1.5 mt-1">
        <span className="text-xs shrink-0" style={{ color: C.textMuted }}>
          {t("model")}
        </span>
        <input
          autoFocus
          aria-label={t("modelIdAria")}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") save();
            if (e.key === "Escape") cancel();
          }}
          className="font-mono text-xs px-1.5 py-1 rounded min-w-0 flex-1"
          style={{
            background: C.bgDeep,
            border: `1px solid ${C.borderAccent}`,
            color: C.textPrimary,
            outline: "none",
          }}
        />
        <button
          onClick={save}
          disabled={mutation.isPending}
          title={t("save")}
          aria-label={t("save")}
          className={iconBtnClass}
          style={iconBtn(C.accent)}
        >
          {mutation.isPending ? (
            <Loader2 size={13} className="animate-spin" />
          ) : (
            <Check size={13} />
          )}
        </button>
        <button
          onClick={cancel}
          disabled={mutation.isPending}
          title={t("cancel")}
          aria-label={t("cancel")}
          className={iconBtnClass}
          style={iconBtn(C.textMuted)}
        >
          <X size={13} />
        </button>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-1.5 mt-1">
      <span className="text-xs shrink-0" style={{ color: C.textMuted }}>
        {t("model")}
      </span>
      <span
        className="font-mono text-xs truncate"
        style={{ color: C.textSecondary }}
        title={runtime.model_identifier ?? undefined}
      >
        {runtime.model_identifier ?? "—"}
      </span>
      <button
        onClick={() => {
          setValue(runtime.model_identifier ?? "");
          setEditing(true);
        }}
        title={t("editModel")}
        aria-label={t("editModel")}
        className={iconBtnClass}
        style={iconBtn(C.textMuted)}
      >
        <Pencil size={12} />
      </button>
    </div>
  );
}

/**
 * Engine-Bezeichnung auf der Karte. Ohne Eintrag fällt sie auf „vLLM Docker"
 * zurück — was für jede neue Engine falsch ist, solange sie hier fehlt.
 */
const RUNTIME_TYPE_LABELS: Record<string, string> = {
  lmstudio: "LM Studio",
  unsloth_porsche: "Unsloth · PORSCHE",
  unsloth: "Unsloth Studio",
  llamacpp_docker: "llama.cpp Docker",
  ssh_process: "Host-Prozess (SSH)",
  openai_compatible: "OpenAI-kompatibel",
  cloud: "Cloud",
  vllm_docker: "vLLM Docker",
};

export function RuntimeCard({ runtime, sizeGb, live }: { runtime: Runtime; sizeGb?: number; live?: RuntimeLiveStatus }) {
  const t = useTranslations("runtimes");
  const queryClient = useQueryClient();
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const isLmStudio = runtime.runtime_type === "lmstudio";
  const lmsKey = (runtime as Runtime & { lms_identifier?: string }).lms_identifier ?? runtime.id;
  const [storedCtx, setStoredCtx] = useState<number | null>(() =>
    isLmStudio ? loadStoredCtx(lmsKey) : null
  );

  const effectiveState = runtime.state ?? "unknown";
  const stateConfig = STATE_CONFIG[effectiveState] ?? STATE_CONFIG.unknown;
  const StateIcon = stateConfig.icon;
  const isLoading = ["starting", "warming"].includes(effectiveState);
  const canStart = effectiveState === "stopped";
  const canStop = effectiveState !== "stopped";

  // Power-managed runtime (unsloth_porsche): box sleeps when idle. The backend
  // reports container_status "asleep" (:5555 down), "booted_no_model" (box awake,
  // model not serving) or "serving" (ready). WoL only wakes the box; the model is
  // loaded on demand via Start. See the design doc for the demand-driven lifecycle.
  const isPowerManaged = runtime.power_managed === true;
  const isAsleep = isPowerManaged && runtime.container_status === "asleep";
  const isBootedNoModel = isPowerManaged && runtime.container_status === "booted_no_model";

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["runtimes"] });

  const startMutation = useMutation({
    mutationFn: () => api.runtimes.start(runtime.id, storedCtx ?? undefined),
    onSuccess: (data) => { setActionMsg(data.message); invalidate(); },
    onError: () => setActionMsg(t("startFailed")),
  });

  const stopMutation = useMutation({
    mutationFn: () => api.runtimes.stop(runtime.id),
    onSuccess: (data) => { setActionMsg(data.message); invalidate(); },
    onError: () => setActionMsg(t("stopFailed")),
  });

  const restartMutation = useMutation({
    mutationFn: () => api.runtimes.restart(runtime.id),
    onSuccess: (data) => { setActionMsg(data.message); invalidate(); },
    onError: () => setActionMsg(t("restartFailed")),
  });

  const wakeMutation = useMutation({
    mutationFn: () => api.runtimes.wake(runtime.id),
    onSuccess: (data) => { setActionMsg(data.message); invalidate(); },
    onError: () => setActionMsg(t("wakeFailed")),
  });

  const probeMutation = useMutation({
    mutationFn: () => api.runtimes.probeModel(runtime.id),
    onSuccess: (data) => {
      const msg = data.changed
        ? t("modelChanged", { old: data.old_model_identifier ?? "—", new: data.new_model_identifier ?? "—" })
        : t("modelUnchanged", { model: data.new_model_identifier ?? "—" });
      setActionMsg(msg);
      queryClient.invalidateQueries({ queryKey: ["runtimes"] });
      queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: () => setActionMsg(t("probeFailed")),
  });

  const isProbeable = ["vllm_docker", "lmstudio", "openai_compatible", "unsloth", "unsloth_porsche"].includes(runtime.runtime_type);

  const isMutating =
    startMutation.isPending || stopMutation.isPending || restartMutation.isPending ||
    probeMutation.isPending || wakeMutation.isPending;

  const accentColor = stateConfig.dot;

  // PR #285's map, not a ternary: a missing entry used to print "vLLM Docker"
  // for every engine it did not know, ssh_process included.
  const engineLabel = RUNTIME_TYPE_LABELS[runtime.runtime_type] ?? "vLLM Docker";

  return (
    <div className="flex flex-col gap-1.5">
      <ListRow
        testId="runtime-row"
        dataAttrs={{ "data-slug": runtime.slug ?? runtime.id, "data-state": effectiveState }}
        tone={
          effectiveState === "ready"
            ? "ok"
            : effectiveState === "failed"
              ? "error"
              : isLoading || isBootedNoModel
                ? "warn"
                : "idle"
        }
        name={runtime.display_name}
        // At 390px: state, engine, host. Everything else is one tap away.
        summary={[t(stateConfig.labelKey), engineLabel, runtime.host?.slug]
          .filter(Boolean)
          .join(" · ")}
        nameSuffix={
          runtime.api_key_secret_id ? (
            <span title={t("apiKeyStored")} className="shrink-0 leading-none">
              <EntityIcon value="🔑" size={12} />
            </span>
          ) : undefined
        }
        chips={
          <>
            {/* state → type → size/detail, the page-wide chip order */}
            <MetaChip tone={STATE_TONE[effectiveState]}>{t(stateConfig.labelKey)}</MetaChip>
            {isAsleep && <MetaChip tone="idle">{t("sleeping")}</MetaChip>}
            {isBootedNoModel && <MetaChip tone="warn">{t("awakeNoModel")}</MetaChip>}
            <MetaChip tone="idle">{engineLabel}</MetaChip>
            {runtime.host && (
              <MetaChip tone="idle" title={t("hostTitle", { name: runtime.host.display_name })}>
                {runtime.host.slug}
              </MetaChip>
            )}
            {/* The context window is shown for EVERY runtime that has one, not
                just vllm_docker: it is rendered into the agents' env
                (OMP_CONTEXT_WINDOW), so a wrong value misconfigures turns
                whatever the engine type. When the live probe disagrees with
                the row, the engine wins the display and the chip says so —
                the row catches up on the watcher's next confirmation. */}
            {runtime.max_context_len > 0 && (
              <MetaChip
                tone={live?.context_drift ? "warn" : "idle"}
                className="tabular-nums"
                title={
                  live?.context_drift
                    ? t("contextDriftTitle", { stored: fmtCtx(runtime.max_context_len) })
                    : undefined
                }
              >
                {fmtCtx(live?.served_context_len ?? runtime.max_context_len)} ctx
              </MetaChip>
            )}
            {runtime.display_name_drift && runtime.display_name_drift.length > 0 && (
              <MetaChip
                tone="warn"
                title={t("nameDriftTitle", {
                  versions: runtime.display_name_drift.join(", "),
                  model: runtime.model_identifier ?? "—",
                })}
              >
                {t("nameDrift")}
              </MetaChip>
            )}
            {isLmStudio && storedCtx && (
              <MetaChip tone="idle" className="tabular-nums">
                {fmtCtx(storedCtx)} ctx
              </MetaChip>
            )}
            {runtime.autostart_supported && <AutostartToggle slug={runtime.slug ?? runtime.id} />}
          </>
        }
        meta={
          sizeGb != null && sizeGb > 0 ? (
            <MetaText className="tabular-nums shrink-0">{sizeGb.toFixed(1)} GB</MetaText>
          ) : undefined
        }
        detail={
          <>
            {live && (
              <span className="flex items-center gap-1.5 text-[11px]" style={{ color: C.textSecondary }}>
                {!live.reachable && live.status === "switching" ? (
                  <MetaChip tone="warn" title={t("switchingTitle")}>
                    {live.phase === "evicting" ? t("switchingEvicting") : t("switchingLoading")}
                  </MetaChip>
                ) : live.reachable ? (
                  <>
                    <span className="truncate" title={live.served_model ?? undefined}>
                      {t("engineServes", { model: live.served_model ?? "—" })}
                    </span>
                    {live.drift && (
                      <MetaChip tone="warn" title={t("driftTitle", { model: runtime.model_identifier ?? "—" })}>
                        {t("drift")}
                      </MetaChip>
                    )}
                  </>
                ) : (
                  <span style={{ color: STATUS_TEXT.error }}>
                    {t("engineUnreachable", { count: live.consecutive_failures })}
                  </span>
                )}
              </span>
            )}
            <BoundAgents runtime={runtime} />
            {!isProbeable && <RuntimeModelEditor runtime={runtime} onMessage={setActionMsg} />}
          </>
        }
        action={
          canStart ? (
            <ActionButton
              icon={Play}
              label={t("start")}
              disabled={isMutating}
              onClick={() => startMutation.mutate()}
              loading={startMutation.isPending}
              variant="success"
            />
          ) : (
            <ActionButton
              icon={Square}
              label={t("stop")}
              disabled={!canStop || isMutating}
              onClick={() => stopMutation.mutate()}
              loading={stopMutation.isPending}
              variant="danger"
            />
          )
        }
        overflow={
          <>
            <OverflowMenu
              label={t("moreActions", { name: runtime.display_name })}
              testId={`runtime-more-${runtime.slug ?? runtime.id}`}
              actions={[
                ...(isPowerManaged
                  ? [{
                      id: "wake",
                      label: t("wake"),
                      icon: Power,
                      disabled: (!isAsleep && effectiveState === "ready") || isMutating,
                      loading: wakeMutation.isPending,
                      onClick: () => wakeMutation.mutate(),
                    }]
                  : []),
                ...(runtime.runtime_type !== "lmstudio"
                  ? [{
                      id: "restart",
                      label: t("restart"),
                      icon: RotateCcw,
                      disabled: !canStop || isMutating,
                      loading: restartMutation.isPending,
                      onClick: () => restartMutation.mutate(),
                    }]
                  : []),
                ...(isProbeable
                  ? [{
                      id: "reprobe",
                      label: t("reprobe"),
                      icon: RefreshCw,
                      disabled: isMutating,
                      loading: probeMutation.isPending,
                      onClick: () => probeMutation.mutate(),
                    }]
                  : []),
                ...(isLmStudio
                  ? [{
                      id: "ctx",
                      label: t("ctxSettings"),
                      icon: Settings2,
                      onClick: () => setSettingsOpen((v) => !v),
                    }]
                  : []),
              ]}
            />
            {runtime.runtime_type === "vllm_docker" && <SparkRecipeSwitcher runtimeId={runtime.id} />}
          </>
        }
      />

      {settingsOpen && isLmStudio && (
        <ContextSettingsPanel
          modelId={lmsKey}
          initialCtx={storedCtx}
          onClose={() => {
            setStoredCtx(loadStoredCtx(lmsKey));
            setSettingsOpen(false);
          }}
        />
      )}

      {actionMsg && <RowNote>{actionMsg}</RowNote>}
    </div>
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
    <div className="shrink-0">
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
      {tab === "download" && <ModelCatalog />}
    </Section>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function RuntimesPage() {
  const t = useTranslations("runtimes");
  const tHosts = useTranslations("runtimes.hosts");
  const tModels = useTranslations("runtimes.models");
  const tInfra = useTranslations("runtimes.infrastructure");
  const queryClient = useQueryClient();
  const [addOpen, setAddOpen] = useState(false);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["runtimes"],
    queryFn: () => api.runtimes.list(),
    refetchInterval: 15_000,
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
  const lmsRuntimes = data?.runtimes.filter((rt) => rt.runtime_type === "lmstudio") ?? [];
  const vllmRuntimes = data?.runtimes.filter((rt) => rt.runtime_type === "vllm_docker") ?? [];

  // Models that already appear as a RuntimeCard → filter out of the LMStudioModelCard list
  const configuredLmsIds = new Set(lmsRuntimes.map((r) => r.lms_identifier).filter(Boolean));
  const unattachedModels = (lmsData?.models ?? []).filter(
    (m) => !configuredLmsIds.has(m.id)
  );

  const addRuntimeMutation = useMutation({
    mutationFn: (model: LMStudioModel) =>
      api.runtimes.addLmstudio({ lms_identifier: model.id, display_name: model.display_name }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["runtimes"] }),
  });

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
              {t("subtitle")}
            </p>
          </div>

          <div className="flex items-center gap-2">
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
            <button
              onClick={() => refetch()}
              className="flex items-center gap-1.5 text-xs px-3 py-2 sm:py-1.5 min-h-11 sm:min-h-0 rounded-md transition-all cursor-pointer"
              style={{
                color: C.textMuted,
                border: `1px solid ${C.borderSubtle}`,
                background: C.borderSubtle,
              }}
            >
              <RotateCcw size={11} />
              {t("refresh")}
            </button>
          </div>
        </div>

        {/* Host metrics — one bar per enabled host (ADR-048) */}
        <HostMetricsBar />

        {/* Three stations, top to bottom: what is running → where models come
            from → the boxes underneath. The jump bar mirrors exactly these. */}
        <SectionNav
          items={[
            { id: "runtimes", label: t("title"), count: allRuntimes.length + unattachedModels.length },
            { id: "models", label: tModels("title") },
            { id: "infrastructure", label: tInfra("title") },
          ]}
        />

        {/* ── A · What is running right now ───────────────────────────────── */}
        <Section
          id="runtimes"
          title={t("title")}
          hint={t("subtitle")}
          count={allRuntimes.length + unattachedModels.length}
          actions={<KvResetScheduleToggle />}
        >
          {isLoading && (
            <div className="flex items-center gap-2 py-2" style={{ color: C.textMuted }}>
              <Loader2 size={13} className="animate-spin" />
              <span className="text-xs">{t("loading")}</span>
            </div>
          )}

          {error && (
            <div
              className="flex items-center gap-2 text-xs px-2.5 py-2 rounded-md"
              style={{ color: STATUS_TEXT.error, background: "transparent", border: `1px solid ${C.error}40` }}
            >
              <AlertCircle size={13} />
              {t("loadError")}
            </div>
          )}

          <VllmContainerCatalog />
          <ActiveDownloads />

          {/* The engine is a chip on the row now, so vLLM and LM Studio no
              longer need to be two sections with two different headers. */}
          {(() => {
            const lmsSizeMap = new Map((lmsData?.models ?? []).map((m) => [m.id, m.size_gb]));
            const getSizeGb = (rt: Runtime) => lmsSizeMap.get(rt.lms_identifier ?? "") ?? undefined;
            const activeRuntimes = allRuntimes.filter((rt) => rt.state !== "stopped");
            const inactiveRuntimes = allRuntimes.filter((rt) => rt.state === "stopped");
            const activeModels = unattachedModels.filter((m) => m.is_loaded);
            const inactiveModels = unattachedModels.filter((m) => !m.is_loaded);
            const groups: [string, Runtime[], typeof activeModels][] = [
              [t("active"), activeRuntimes, activeModels],
              [t("inactive"), inactiveRuntimes, inactiveModels],
            ];
            return (
              <>
                {groups.map(([label, runtimes, models]) =>
                  runtimes.length + models.length === 0 ? null : (
                    <div key={label} className="mb-3 last:mb-0">
                      <div className="label-sys mb-1.5">{label}</div>
                      <div className="flex flex-col gap-1.5">
                        {runtimes.map((rt) => (
                          <RuntimeCard
                            key={rt.id}
                            runtime={rt}
                            sizeGb={getSizeGb(rt)}
                            live={liveData?.live?.[rt.slug ?? rt.id]}
                          />
                        ))}
                        {models.map((m) => (
                          <LMStudioModelCard key={m.id} model={m} />
                        ))}
                      </div>
                    </div>
                  ),
                )}
                {data && allRuntimes.length + unattachedModels.length === 0 && (
                  <div className="text-xs text-center py-8" style={{ color: C.textMuted }}>
                    {t("noVllm")}
                  </div>
                )}
              </>
            );
          })()}

          {/* The download panel lives in the Models section now. */}
          <button
            type="button"
            onClick={() => openModelsTab("download")}
            data-testid="lms-download-pointer"
            className="inline-flex items-center gap-1.5 mt-2 rounded-md px-2 py-2 sm:py-1 min-h-11 sm:min-h-0 text-xs cursor-pointer transition-colors hover:bg-[var(--color-bg-surface)]"
            style={{ color: C.textMuted }}
          >
            <Download size={11} />
            {t("downloadMoved")}
          </button>
        </Section>

        {/* ── B · Where models come from ──────────────────────────────────── */}
        <ModelsSection />

        {/* ── C · The boxes underneath — quieter, both lists in one place ─── */}
        <Section id="infrastructure" title={tInfra("title")} hint={tInfra("subtitle")}>
          <div className="flex flex-col gap-5">
            <HostsSection embedded />
            <CliToolsSection embedded />
          </div>
        </Section>
      </div>

      <AddRuntimeModal open={addOpen} onClose={() => setAddOpen(false)} />
    </AppShell>
  );
}

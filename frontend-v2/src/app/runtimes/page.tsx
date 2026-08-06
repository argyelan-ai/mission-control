"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { motion } from "framer-motion";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
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
import { cn } from "@/lib/utils";
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
import { Section, SectionNav } from "@/components/shared/Section";

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
      className="action-btn flex items-center justify-center w-7 h-7 min-w-[28px] rounded-md transition-colors cursor-pointer disabled:cursor-not-allowed"
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
                  className="flex items-center justify-center w-6 h-6 rounded-md transition-all cursor-pointer disabled:opacity-40"
                  style={{
                    background: `${C.error}14`,
                    border: `1px solid ${C.error}33`,
                    color: STATUS_TEXT.error,
                  }}
                >
                  {cancelMutation.isPending && cancelMutation.variables === dl.name
                    ? <Loader2 size={10} className="animate-spin" />
                    : <span style={{ fontSize: "12px", lineHeight: 1 }}>✕</span>
                  }
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

function fmtCtx(n: number): string {
  if (n >= 262144) return "262k";
  if (n >= 200000) return "200k";
  if (n >= 131072) return "131k";
  if (n >= 65536) return "65k";
  if (n >= 32768) return "32k";
  if (n >= 16384) return "16k";
  if (n >= 8192) return "8k";
  return "4k";
}

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
    <motion.div
      initial={{ opacity: 0, x: -4 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
      style={{
        background: C.borderSubtle,
        border: `1px solid ${C.borderSubtle}`,
        borderRadius: "10px",
        overflow: "hidden",
        borderLeft: `1px solid ${C.borderSubtle}`,
      }}
    >
      {/* Main row */}
      <div className="flex items-center gap-3 px-3 py-2.5">
        {/* Status dot */}
        <div
          className="w-1.5 h-1.5 rounded-full shrink-0"
          style={{
            background: accentColor,
          }}
        />

        {/* Name + meta */}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-medium text-sm truncate" style={{ color: C.textPrimary }}>
              {model.display_name}
            </span>
            {model.is_embedding && (
              <span
                className="shrink-0"
                style={{
                  background: C.accentSubtle,
                  border: `1px solid ${C.borderAccent}`,
                  color: C.textSecondary,
                  fontSize: "9px",
                  padding: "1px 5px",
                  borderRadius: "4px",
                  letterSpacing: "0.04em",
                }}
              >
                EMBED
              </span>
            )}
          </div>
          <div className="flex items-center gap-1.5 mt-0.5">
            <span className="text-xs tabular-nums" style={{ color: C.textMuted }}>
              {model.size_gb > 0 ? `${model.size_gb.toFixed(1)} GB` : "—"}
            </span>
            <span style={{ color: C.borderSubtle }}>·</span>
            <span className="text-xs" style={{ color: C.textMuted }}>LM Studio</span>
            {storedCtx && (
              <>
                <span style={{ color: C.borderSubtle }}>·</span>
                <span className="text-xs tabular-nums" style={{ color: `${C.accent}99` }}>
                  {fmtCtx(storedCtx)} ctx
                </span>
              </>
            )}
          </div>
        </div>

        {/* Actions */}
        <div className="flex items-center gap-1 shrink-0">
          {/* Gear settings button */}
          {!model.is_embedding && (
            <button
              onClick={() => setSettingsOpen((o) => !o)}
              title={t("ctxSettings")}
              aria-label={t("ctxSettings")}
              className="flex items-center justify-center w-7 h-7 rounded-lg transition-all cursor-pointer"
              style={{
                background: settingsOpen ? C.border : "transparent",
                border: `1px solid ${settingsOpen ? C.borderActive : "transparent"}`,
                color: settingsOpen ? C.textSecondary : C.borderActive,
              }}
            >
              <Settings2 size={12} />
            </button>
          )}
          <ActionButton
            icon={Play}
            label={t("load")}
            disabled={model.is_loaded || isMutating}
            onClick={() => loadMutation.mutate()}
            loading={loadMutation.isPending}
            variant="success"
          />
          <ActionButton
            icon={Square}
            label={t("unload")}
            disabled={!model.is_loaded || isMutating}
            onClick={() => unloadMutation.mutate()}
            loading={unloadMutation.isPending}
            variant="danger"
          />
        </div>
      </div>

      {/* Settings panel */}
      {settingsOpen && !model.is_embedding && (
        <ContextSettingsPanel
          modelId={model.id}
          initialCtx={storedCtx}
          onClose={() => setSettingsOpen(false)}
        />
      )}

      {/* Feedback message */}
      {actionMsg && (
        <div
          className="text-xs mx-4 mb-3 px-3 py-2 rounded-lg"
          style={{
            background: C.accentSubtle,
            border: `1px solid ${C.borderAccent}`,
            color: C.textSecondary,
          }}
        >
          {actionMsg}
        </div>
      )}
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
                className="text-xs px-2 py-0.5 rounded cursor-pointer disabled:opacity-40"
                style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.accent }}
              >
                ↓
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

  // Tab-specific colors: LMS = online-green, HF = warning-orange
  const lmsColor = C.online;
  const hfColor = C.warning;

  return (
    <div
      className="mb-6 rounded-xl overflow-hidden"
      style={{ border: `1px solid ${C.borderSubtle}`, background: C.borderSubtle }}
    >
      {/* Header */}
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between px-4 py-3 cursor-pointer"
        style={{ color: C.textSecondary }}
      >
        <div className="flex items-center gap-2 text-sm font-medium">
          <Download size={14} />
          {t("downloadModel")}
        </div>
        {open ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
      </button>

      {open && (
        <div className="px-4 pb-4">
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
                className="flex-1 text-xs py-1.5 rounded-md transition-colors cursor-pointer"
                style={{
                  background: tab === t ? C.borderActive : "transparent",
                  color:
                    tab === t
                      ? t === "lms"
                        ? lmsColor
                        : hfColor
                      : C.textMuted,
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
              className="flex items-center gap-1.5 text-xs mb-3 w-fit"
              style={{ color: C.textMuted }}
            >
              <span>↗</span>
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
              className="flex-1 text-sm px-3 py-2 rounded-lg outline-none"
              style={{
                background: C.border,
                border: `1px solid ${C.borderSubtle}`,
                color: C.textPrimary,
              }}
            />
            <button
              onClick={handleSearch}
              disabled={!query.trim()}
              className="text-xs px-3 py-2 rounded-lg disabled:opacity-40 cursor-pointer disabled:cursor-not-allowed"
              style={{
                background: isLms ? `${lmsColor}1F` : `${hfColor}1F`,
                border: isLms
                  ? `1px solid ${lmsColor}40`
                  : `1px solid ${hfColor}40`,
                color: isLms ? lmsColor : hfColor,
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
              <div className="rounded-lg overflow-hidden" style={{ border: `1px solid ${C.borderSubtle}` }}>
                {catalogData.models.map((m, i) => {
                  const baseName = m.model_id.split("/").pop()?.replace(/-gguf$/i, "").toLowerCase() ?? "";
                  const installed = baseName.length > 0 && installedIds.some((id) => id.toLowerCase().includes(baseName));
                  return (
                    <div key={m.model_id}>
                      <div
                        className="flex items-center justify-between px-3 py-2.5"
                        style={{
                          borderBottom:
                            i < catalogData.models.length - 1 && pickingModel !== m.model_id
                              ? `1px solid ${C.borderSubtle}`
                              : undefined,
                        }}
                      >
                        <div>
                          <div className="text-sm" style={{ color: C.textPrimary }}>
                            {m.name}
                          </div>
                          <div className="text-xs mt-0.5" style={{ color: C.textMuted }}>
                            {[m.params, m.size_gb != null ? `${m.size_gb} GB` : null]
                              .filter(Boolean)
                              .join(" · ")}
                          </div>
                        </div>
                        {installed ? (
                          <div
                            className="text-xs px-2 py-1 rounded"
                            style={{ background: `${C.online}1A`, color: C.online }}
                          >
                            ✓
                          </div>
                        ) : (
                          <button
                            onClick={() => setPickingModel(pickingModel === m.model_id ? null : m.model_id)}
                            className="text-xs px-2.5 py-1 rounded cursor-pointer"
                            style={{
                              background: C.accentSubtle,
                              border: `1px solid ${C.borderAccent}`,
                              color: C.accent,
                            }}
                          >
                            {pickingModel === m.model_id ? "✕" : t("loadArrow")}
                          </button>
                        )}
                      </div>
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
                <div className="rounded-lg overflow-hidden" style={{ border: `1px solid ${C.borderSubtle}` }}>
                  {hfData.files.map((f, i) => (
                    <div
                      key={f.filename}
                      className="flex items-center justify-between px-3 py-2.5"
                      style={{
                        borderBottom:
                          i < hfData.files.length - 1
                            ? `1px solid ${C.borderSubtle}`
                            : undefined,
                      }}
                    >
                      <div>
                        <div className="text-sm" style={{ color: C.textPrimary }}>
                          {f.filename}
                        </div>
                        <div className="text-xs mt-0.5" style={{ color: C.textMuted }}>
                          {f.size_gb} GB
                        </div>
                      </div>
                      <button
                        onClick={() =>
                          downloadHfMutation.mutate({ repoId: submitted, filename: f.filename })
                        }
                        disabled={isMutating}
                        className="text-xs px-2.5 py-1 rounded disabled:opacity-40 cursor-pointer disabled:cursor-not-allowed"
                        style={{
                          background: `${hfColor}1F`,
                          border: `1px solid ${hfColor}40`,
                          color: hfColor,
                        }}
                      >
                        {downloadHfMutation.isPending ? (
                          <Loader2 size={11} className="animate-spin" />
                        ) : (
                          t("loadArrow")
                        )}
                      </button>
                    </div>
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
              className="inline-flex items-center gap-1 px-1.5 py-1 rounded-md font-mono text-[10px] leading-none min-h-[24px] hover:bg-[var(--color-bg-hover)] transition-colors cursor-pointer"
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
              <span
                className="rounded px-1 py-0.5 text-[10px]"
                style={{ color: STATUS_TEXT.warning, border: `1px solid ${STATUS.warning}` }}
                title={t("pendingSyncTitle")}
              >
                {t("pendingSync")}
              </span>
            )}
          </span>
        ))}
        <button
          onClick={() => setBindOpen(true)}
          title={t("bindAgent")}
          className="inline-flex items-center gap-1 px-1.5 py-1 rounded-md text-[10px] leading-none min-h-[24px] cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
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

  const iconBtn = (color: string) => ({
    padding: "3px",
    borderRadius: "5px",
    background: "transparent" as const,
    border: "1px solid transparent",
    color,
    cursor: "pointer" as const,
    display: "flex" as const,
    alignItems: "center" as const,
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

  return (
    <motion.div
      initial={{ opacity: 0, x: -4 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
      style={{
        background: C.borderSubtle,
        border: `1px solid ${C.borderSubtle}`,
        borderRadius: "10px",
        overflow: "hidden",
      }}
    >
      {/* Main row — mobile 2-line layout: name/meta on top, actions below right
          (a single row squeezed the name against 5 buttons at 390px) */}
      <div className="flex flex-col gap-2 px-3 py-2.5 sm:flex-row sm:items-center sm:gap-3">
        <div className="flex items-center gap-3 min-w-0 sm:flex-1">
        {/* Status dot */}
        <div
          className="w-1.5 h-1.5 rounded-full shrink-0"
          style={{
            background: accentColor,
          }}
        />
        {/* Name + meta */}
        <div className="min-w-0 flex-1">
          <div className="font-medium text-sm truncate flex items-center gap-1.5" style={{ color: C.textPrimary }}>
            <span className="truncate">{runtime.display_name}</span>
            {runtime.api_key_secret_id && (
              <span title={t("apiKeyStored")} className="shrink-0 leading-none">
                <EntityIcon value="🔑" size={12} />
              </span>
            )}
          </div>
          <div className="flex items-center gap-x-1.5 gap-y-1 mt-1 flex-wrap">
            {sizeGb != null && sizeGb > 0 && (
              <>
                <span className="text-xs tabular-nums" style={{ color: C.textMuted }}>
                  {sizeGb.toFixed(1)} GB
                </span>
                <span style={{ color: C.borderSubtle }}>·</span>
              </>
            )}
            <span className="text-xs" style={{ color: C.textMuted }}>
              {RUNTIME_TYPE_LABELS[runtime.runtime_type] ?? "vLLM Docker"}
            </span>
            {/* Host chip (ADR-048) — only when the runtime is bound to a host */}
            {runtime.host && (
              <>
                <span style={{ color: C.borderSubtle }}>·</span>
                <span
                  className="text-[10px] font-mono px-1.5 py-px rounded shrink-0"
                  style={{
                    background: C.accentSubtle,
                    border: `1px solid ${C.borderAccent}`,
                    color: C.textSecondary,
                  }}
                  title={t("hostTitle", { name: runtime.host.display_name })}
                >
                  {runtime.host.slug}
                </span>
              </>
            )}
            {/* Power-managed honest status: distinguishes "asleep" from
                "awake but model not loaded" — the bare STATE_CONFIG label
                ("Gestoppt") would hide that difference. */}
            {isAsleep && (
              <>
                <span style={{ color: C.borderSubtle }}>·</span>
                <span className="text-xs" style={{ color: C.textDim }}>
                  {t("sleeping")}
                </span>
              </>
            )}
            {isBootedNoModel && (
              <>
                <span style={{ color: C.borderSubtle }}>·</span>
                <span className="text-xs" style={{ color: STATUS_TEXT.warning }}>
                  {t("awakeNoModel")}
                </span>
              </>
            )}
            {runtime.runtime_type === "vllm_docker" && runtime.max_context_len > 0 && (
              <>
                <span style={{ color: C.borderSubtle }}>·</span>
                <span className="text-xs tabular-nums" style={{ color: C.textMuted }}>
                  {(runtime.max_context_len / 1000).toFixed(0)}K ctx
                </span>
              </>
            )}
            {isLmStudio && storedCtx && (
              <>
                <span style={{ color: C.borderSubtle }}>·</span>
                <span className="text-xs tabular-nums" style={{ color: C.online }}>
                  {fmtCtx(storedCtx)} ctx
                </span>
              </>
            )}
            {runtime.autostart_supported && (
              <>
                <span style={{ color: C.borderSubtle }}>·</span>
                <AutostartToggle slug={runtime.slug ?? runtime.id} />
              </>
            )}
            <span style={{ color: C.borderSubtle }}>·</span>
            <BoundAgents runtime={runtime} />
          </div>
          {!isProbeable && (
            <RuntimeModelEditor runtime={runtime} onMessage={setActionMsg} />
          )}
          {live && (
            <div className="flex items-center gap-2 text-xs mt-0.5" style={{ color: C.textSecondary }}>
              <span
                className="inline-block h-1.5 w-1.5 rounded-full shrink-0"
                style={{
                  background: live.reachable
                    ? STATUS.online
                    : live.status === "switching"
                      ? STATUS.warning
                      : STATUS.error,
                }}
              />
              {/* Planned downtime (recipe switch / start / cold load) reads as
                  a warning chip, not the red "unreachable" line — the engine is
                  down on purpose and comes back in 2-15 minutes. */}
              {!live.reachable && live.status === "switching" ? (
                <span
                  className="rounded px-1.5 py-0.5 text-[10px] font-medium shrink-0"
                  style={{ color: STATUS_TEXT.warning, border: `1px solid ${STATUS.warning}` }}
                  title={t("switchingTitle")}
                >
                  {live.phase === "evicting" ? t("switchingEvicting") : t("switchingLoading")}
                </span>
              ) : live.reachable ? (
                <>
                  <span className="truncate" title={live.served_model ?? undefined}>
                    {t("engineServes", { model: live.served_model ?? "—" })}
                  </span>
                  {live.drift && (
                    <span
                      className="rounded px-1.5 py-0.5 text-[10px] font-medium shrink-0"
                      style={{ color: STATUS_TEXT.warning, border: `1px solid ${STATUS.warning}` }}
                      title={t("driftTitle", { model: runtime.model_identifier ?? "—" })}
                    >
                      {t("drift")}
                    </span>
                  )}
                </>
              ) : (
                <span style={{ color: STATUS_TEXT.error }}>
                  {t("engineUnreachable", { count: live.consecutive_failures })}
                </span>
              )}
            </div>
          )}
        </div>

        </div>

        {/* Actions — the one action that matches the current state stays on the
            row; everything else moves behind the overflow menu. Seven icon
            buttons per card were mostly disabled and unreadable. */}
        <div className="flex items-center gap-1.5 shrink-0 self-end sm:self-auto">
          {canStart ? (
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
          )}
          <OverflowMenu
            label={t("moreActions", { name: runtime.display_name })}
            testId={`runtime-more-${runtime.slug ?? runtime.id}`}
            actions={[
              ...(isPowerManaged
                ? [{
                    id: "wake",
                    label: t("wake"),
                    icon: Power,
                    // WoL is cheap and idempotent — offer it whenever the box
                    // is not already serving.
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
          {runtime.runtime_type === "vllm_docker" && (
            <SparkRecipeSwitcher runtimeId={runtime.id} />
          )}
        </div>
      </div>

      {/* Context Settings Panel */}
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

      {/* Feedback message */}
      {actionMsg && (
        <div
          className="text-xs mx-4 mb-3 px-3 py-2 rounded-lg"
          style={{
            background: C.accentSubtle,
            border: `1px solid ${C.borderAccent}`,
            color: C.textSecondary,
          }}
        >
          {actionMsg}
        </div>
      )}

    </motion.div>
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
        className="flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg transition-all cursor-pointer"
        style={{
          background: open ? `${C.warning}1A` : C.borderSubtle,
          border: open ? `1px solid ${C.warning}4D` : `1px solid ${C.borderSubtle}`,
          color: open ? C.warning : C.textMuted,
        }}
        title={t("scheduleTitle")}
      >
        ⏱ {t("toggle")}
        {activeSchedule && (
          <span
            className="text-xs px-1 rounded"
            style={{ background: `${C.online}1F`, color: C.online, fontSize: "9px" }}
          >
            {activeSchedule.time_of_day}
          </span>
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

function ModelsSection() {
  const t = useTranslations("runtimes.models");
  const [tab, setTab] = useState<ModelsTab>("providers");

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
      count={providerCount + recipeCount}
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
              className="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs cursor-pointer transition-colors"
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
  const tCli = useTranslations("runtimes.cliTools");
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
              className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg transition-all cursor-pointer"
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
              className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg transition-all cursor-pointer"
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

        {/* Jump bar — the page runs past 2300 px, so CLI-Tools used to be
            reachable only by scrolling through everything above it. */}
        <SectionNav
          items={[
            { id: "vllm", label: "vLLM Docker", count: vllmRuntimes.length },
            { id: "lmstudio", label: "LM Studio", count: lmsRuntimes.length + unattachedModels.length },
            { id: "hosts", label: tHosts("title") },
            { id: "models", label: tModels("title") },
            { id: "cli-tools", label: tCli("title") },
          ]}
        />

        {/* vLLM Docker section */}
        <Section id="vllm" title="vLLM Docker" hint={t("vllmHint")} count={vllmRuntimes.length}>

          {isLoading && (
            <div className="flex items-center gap-2 py-2" style={{ color: C.textMuted }}>
              <Loader2 size={13} className="animate-spin" />
              <span className="text-xs">{t("loading")}</span>
            </div>
          )}

          {error && (
            <div className="flex items-center gap-2 text-xs px-4 py-3 rounded-xl" style={{ color: STATUS_TEXT.error, background: `${C.error}0F`, border: `1px solid ${C.error}26` }}>
              <AlertCircle size={13} />
              {t("loadError")}
            </div>
          )}

          <VllmContainerCatalog />

          {data && (
            <div className="flex flex-col gap-2">
              {vllmRuntimes.map((rt) => (
                <RuntimeCard key={rt.id} runtime={rt} live={liveData?.live?.[rt.slug ?? rt.id]} />
              ))}
              {vllmRuntimes.length === 0 && (
                <div className="text-xs text-center py-10" style={{ color: C.textMuted }}>
                  {t("noVllm")}
                </div>
              )}
            </div>
          )}
        </Section>

        {/* LM Studio section */}
        <Section
          id="lmstudio"
          title="LM Studio"
          hint={t("lmsHint")}
          count={lmsRuntimes.length + unattachedModels.length}
          actions={<KvResetScheduleToggle />}
        >

          <ActiveDownloads />

          {!lmsData && lmsRuntimes.length === 0 && (
            <div className="flex items-center gap-2 py-2" style={{ color: C.textMuted }}>
              <Loader2 size={13} className="animate-spin" />
              <span className="text-xs">{t("connecting")}</span>
            </div>
          )}

          {/* Active / Inactive sections */}
          {(() => {
            const lmsSizeMap = new Map((lmsData?.models ?? []).map((m) => [m.id, m.size_gb]));
            const getSizeGb = (rt: Runtime) => lmsSizeMap.get(rt.lms_identifier ?? "") ?? undefined;
            const activeRuntimes = lmsRuntimes.filter((rt) => rt.state !== "stopped");
            const inactiveRuntimes = lmsRuntimes.filter((rt) => rt.state === "stopped");
            const activeModels = unattachedModels.filter((m) => m.is_loaded);
            const inactiveModels = unattachedModels.filter((m) => !m.is_loaded);
            const hasActive = activeRuntimes.length > 0 || activeModels.length > 0;
            const hasInactive = inactiveRuntimes.length > 0 || inactiveModels.length > 0;

            return (
              <>
                {hasActive && (
                  <div className="mb-3">
                    <div className="flex items-center gap-2 mb-2 px-0.5">
                      <span className="text-xs font-medium tracking-wider uppercase" style={{ color: C.online, letterSpacing: "0.07em", fontSize: "10px" }}>{t("active")}</span>
                      <div className="flex-1 h-px" style={{ background: `${C.online}26` }} />
                    </div>
                    <div className="flex flex-col gap-2">
                      {activeRuntimes.map((rt) => <RuntimeCard key={rt.id} runtime={rt} sizeGb={getSizeGb(rt)} live={liveData?.live?.[rt.slug ?? rt.id]} />)}
                      {activeModels.map((model) => <LMStudioModelCard key={model.id} model={model} />)}
                    </div>
                  </div>
                )}
                {hasInactive && (
                  <div>
                    <div className="flex items-center gap-2 mb-2 px-0.5">
                      <span className="text-xs font-medium tracking-wider uppercase" style={{ color: C.textMuted, letterSpacing: "0.07em", fontSize: "10px" }}>{t("inactive")}</span>
                      <div className="flex-1 h-px" style={{ background: C.border }} />
                    </div>
                    <div className="flex flex-col gap-2">
                      {inactiveRuntimes.map((rt) => <RuntimeCard key={rt.id} runtime={rt} sizeGb={getSizeGb(rt)} live={liveData?.live?.[rt.slug ?? rt.id]} />)}
                      {inactiveModels.map((model) => <LMStudioModelCard key={model.id} model={model} />)}
                    </div>
                  </div>
                )}
              </>
            );
          })()}
        </Section>

        {/* Hosts Registry (ADR-048) */}
        <HostsSection />

        {/* Modelle — eine Sektion, drei Sichten. Vorher waren „Model catalog"
            (was es beim Anbieter gibt), „Local models" (was auf eigener
            Hardware läuft) und „Download model" drei getrennte Blöcke, die
            zusammen über die Hälfte der Seitenhöhe belegten. Nur die gewählte
            Sicht rendert; Funktionen und Endpunkte bleiben unverändert. */}
        <ModelsSection />

        {/* CLI-Tools (festgebackene Agent-Werkzeuge) */}
        <CliToolsSection />
      </div>

      <AddRuntimeModal open={addOpen} onClose={() => setAddOpen(false)} />
    </AppShell>
  );
}

"use client";

import { useState } from "react";
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
import { BindAgentModal } from "@/components/shared/BindAgentModal";
import { SparkRecipeSwitcher } from "@/components/shared/SparkRecipeSwitcher";
import Link from "next/link";
import { Plug } from "lucide-react";
import { C, STATUS, STATUS_TEXT } from "@/lib/colors";

// ── State Configuration ───────────────────────────────────────────────────────

const STATE_CONFIG: Record<
  RuntimeState,
  { label: string; color: string; dot: string; icon: LucideIcon }
> = {
  ready: {
    label: "Ready",
    color: C.online,
    dot: C.online,
    icon: CheckCircle2,
  },
  warming: {
    label: "Warmup...",
    color: C.warning,
    dot: C.warning,
    icon: Clock,
  },
  starting: {
    label: "Starting...",
    color: C.info,
    dot: C.info,
    icon: Loader2,
  },
  stopped: {
    label: "Stopped",
    color: C.textMuted,
    dot: STATUS.offline,
    icon: Square,
  },
  failed: {
    label: "Error",
    color: C.error,
    dot: C.error,
    icon: AlertCircle,
  },
  unknown: {
    label: "Unknown",
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
  const colors = {
    success: { bg: `${C.online}14`, border: `${C.online}33`, text: C.online },
    danger:  { bg: `${C.error}14`, border: `${C.error}33`, text: C.error },
    default: { bg: C.borderSubtle, border: C.borderSubtle, text: C.textMuted },
  };
  const c = colors[variant];

  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={label}
      className="flex items-center justify-center w-7 h-7 rounded-lg transition-all cursor-pointer disabled:cursor-not-allowed"
      style={{
        background: disabled ? "transparent" : c.bg,
        border: `1px solid ${disabled ? "transparent" : c.border}`,
        color: disabled ? C.borderActive : c.text,
      }}
    >
      {loading ? <Loader2 size={12} className="animate-spin" /> : <Icon size={12} />}
    </button>
  );
}

// ── Active Downloads Panel ────────────────────────────────────────────────────

function ActiveDownloads() {
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
          Downloads
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
                  title="Cancel"
                  aria-label="Cancel download"
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
            Context Window
          </span>
          <span className="text-xs font-mono tabular-nums" style={{ color: C.textPrimary }}>
            {isStandard ? "Standard (65k)" : `${selected!.toLocaleString()} tokens`}
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
            Standard
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
            aria-label="Context window preset"
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
              Custom:
            </span>
            <input
              type="text"
              inputMode="numeric"
              placeholder="e.g. 200000"
              value={customInput}
              disabled={isStandard}
              aria-label="Custom context value"
              onChange={(e) => handleCustomInput(e.target.value)}
              className="flex-1 text-xs font-mono px-2 py-1 rounded disabled:opacity-30"
              style={{
                background: C.borderSubtle,
                border: `1px solid ${customError ? C.error : C.border}`,
                color: customError ? STATUS_TEXT.error : C.textPrimary,
                minWidth: 0,
              }}
            />
            <span style={{ color: C.textDim, fontSize: "10px" }}>tokens</span>
          </div>
          {customError && (
            <span style={{ color: STATUS_TEXT.error, fontSize: "10px" }}>512 – 1'048'576</span>
          )}
        </div>

        {/* Hint + Save */}
        <div className="flex items-center justify-between gap-2">
          <span style={{ color: C.textDim, fontSize: "10px" }}>
            {isStandard ? "Uses LM Studio global setting" : "Applied on next load"}
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
            Save
          </button>
        </div>
      </div>
    </motion.div>
  );
}

// ── LM Studio Model Row ───────────────────────────────────────────────────────

function LMStudioModelCard({ model }: { model: LMStudioModel }) {
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
    onError: () => setActionMsg("Load failed."),
  });

  const unloadMutation = useMutation({
    mutationFn: () => api.lmstudio.unload(model.id),
    onSuccess: (data) => { setActionMsg(data.message); invalidate(); },
    onError: () => setActionMsg("Unload failed."),
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
              title="Context settings"
              aria-label="Context settings"
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
            label="Load"
            disabled={model.is_loaded || isMutating}
            onClick={() => loadMutation.mutate()}
            loading={loadMutation.isPending}
            variant="success"
          />
          <ActionButton
            icon={Square}
            label="Unload"
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
          <Loader2 size={11} className="animate-spin" /> Loading variants...
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
        <div className="px-3 py-2 text-xs" style={{ color: C.textMuted }}>No GGUF variants found</div>
      )}
    </div>
  );
}

// ── Model Catalog ─────────────────────────────────────────────────────────────

function ModelCatalog() {
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
      setMessage("Failed to start download.");
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
      setMessage("Failed to start download.");
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
          Download model
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
              Open lmstudio.ai/models
            </a>
          )}

          {/* Search field */}
          <div className="flex gap-2 mb-4">
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSearch()}
              placeholder={
                isLms
                  ? "qwen, llama, mistral..."
                  : "Repo ID (e.g. Jackrong/Qwen3.5-27B-GGUF)"
              }
              aria-label={isLms ? "Search LM Studio model" : "HuggingFace repo ID"}
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
              Search
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
                Searching...
              </div>
            ) : !catalogData?.models.length ? (
              <div className="text-xs text-center py-4" style={{ color: C.textMuted }}>
                No results for &ldquo;{submitted}&rdquo;
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
                            {pickingModel === m.model_id ? "✕" : "↓ Load"}
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
                Loading repo...
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
                  {hfData.name} · {hfData.files.length} files
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
                          "↓ Load"
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

// ── Bound Agents Footer (Phase 15 T3.3) ─────────────────────────────────
// Shows the agents currently using this runtime + a "Bind Agent" button
// that opens BindAgentModal. Only visible for runtimes that have a slug
// (DB-managed); legacy JSON runtimes are skipped.

function BoundAgentsFooter({ runtime }: { runtime: Runtime }) {
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

  return (
    <>
      <div
        className="px-3 py-2 border-t flex items-center gap-2 flex-wrap"
        style={{ borderColor: C.borderSubtle }}
      >
        <span
          className="text-[10px] font-mono uppercase tracking-wider"
          style={{ color: C.textMuted }}
        >
          Agents
        </span>
        {isLoading && <Loader2 size={11} className="animate-spin" style={{ color: C.textMuted }} />}
        {!isLoading && bound.length === 0 && (
          <span className="text-[11px]" style={{ color: C.textMuted }}>
            none — unbound
          </span>
        )}
        {bound.map((a) => (
          <span key={a.id} className="inline-flex items-center gap-1">
            <Link
              href={`/agents/${a.id}`}
              className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md font-mono text-[10px] hover:bg-[rgba(255,255,255,0.06)] transition-colors cursor-pointer"
              style={{
                backgroundColor: C.accentSubtle,
                color: C.textSecondary,
                border: `1px solid ${C.borderAccent}`,
              }}
              title={`${a.name} · ${a.agent_runtime}`}
            >
              🤖 {a.name}
            </Link>
            {a.pending_runtime_sync && (
              <span
                className="rounded px-1 text-[10px]"
                style={{ color: STATUS_TEXT.warning, border: `1px solid ${STATUS.warning}` }}
                title="Model changed while busy — auto-syncs after its current task"
              >
                pending sync
              </span>
            )}
          </span>
        ))}
        <button
          onClick={() => setBindOpen(true)}
          className="ml-auto inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[10px] cursor-pointer transition-colors hover:bg-[rgba(255,255,255,0.04)]"
          style={{
            color: C.accent,
            border: `1px dashed ${C.borderAccent}`,
          }}
        >
          <Plug size={10} />
          Bind Agent
        </button>
      </div>

      {anyPending && (
        <div
          className="px-3 pb-2 flex items-center gap-2 text-[11px]"
          style={{ color: C.textSecondary }}
        >
          <span>Model sync pending — runs automatically when the agent is idle.</span>
          <button
            onClick={() => syncNowMutation.mutate()}
            className="underline cursor-pointer"
            style={{ color: STATUS_TEXT.warning }}
            title="Force sync NOW — interrupts a running task"
          >
            Sync now (force)
          </button>
        </div>
      )}

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
      onMessage?.(
        `Modell auf „${data.model_identifier ?? "—"}" gesetzt — gebundene Agents werden beim nächsten Watcher-Tick neu gestartet.`
      );
    },
    onError: () => onMessage?.("Modell-Update fehlgeschlagen."),
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
          Modell:
        </span>
        <input
          autoFocus
          aria-label="Modell-Identifier"
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
          title="Speichern"
          aria-label="Speichern"
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
          title="Abbrechen"
          aria-label="Abbrechen"
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
        Modell:
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
        title="Modell bearbeiten"
        aria-label="Modell bearbeiten"
        style={iconBtn(C.textMuted)}
      >
        <Pencil size={12} />
      </button>
    </div>
  );
}

export function RuntimeCard({ runtime, sizeGb, live }: { runtime: Runtime; sizeGb?: number; live?: RuntimeLiveStatus }) {
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
    onError: () => setActionMsg("Start failed."),
  });

  const stopMutation = useMutation({
    mutationFn: () => api.runtimes.stop(runtime.id),
    onSuccess: (data) => { setActionMsg(data.message); invalidate(); },
    onError: () => setActionMsg("Stop failed."),
  });

  const restartMutation = useMutation({
    mutationFn: () => api.runtimes.restart(runtime.id),
    onSuccess: (data) => { setActionMsg(data.message); invalidate(); },
    onError: () => setActionMsg("Restart failed."),
  });

  const wakeMutation = useMutation({
    mutationFn: () => api.runtimes.wake(runtime.id),
    onSuccess: (data) => { setActionMsg(data.message); invalidate(); },
    onError: () => setActionMsg("Wake failed."),
  });

  const probeMutation = useMutation({
    mutationFn: () => api.runtimes.probeModel(runtime.id),
    onSuccess: (data) => {
      const msg = data.changed
        ? `Model: ${data.old_model_identifier ?? "—"} → ${data.new_model_identifier}`
        : `Model unchanged: ${data.new_model_identifier ?? "—"}`;
      setActionMsg(msg);
      queryClient.invalidateQueries({ queryKey: ["runtimes"] });
      queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: () => setActionMsg("Probe failed."),
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
              <span title="API-Key hinterlegt" className="shrink-0 text-xs leading-none">
                🔑
              </span>
            )}
          </div>
          <div className="flex items-center gap-1.5 mt-0.5">
            {sizeGb != null && sizeGb > 0 && (
              <>
                <span className="text-xs tabular-nums" style={{ color: C.textMuted }}>
                  {sizeGb.toFixed(1)} GB
                </span>
                <span style={{ color: C.borderSubtle }}>·</span>
              </>
            )}
            <span className="text-xs" style={{ color: C.textMuted }}>
              {runtime.runtime_type === "lmstudio"
                ? "LM Studio"
                : runtime.runtime_type === "unsloth_porsche"
                  ? "Unsloth · PORSCHE"
                  : "vLLM Docker"}
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
                  title={`Host: ${runtime.host.display_name}`}
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
                  Sleeping
                </span>
              </>
            )}
            {isBootedNoModel && (
              <>
                <span style={{ color: C.borderSubtle }}>·</span>
                <span className="text-xs" style={{ color: STATUS_TEXT.warning }}>
                  Awake — model not loaded (Start)
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
          </div>
          {!isProbeable && (
            <RuntimeModelEditor runtime={runtime} onMessage={setActionMsg} />
          )}
          {live && (
            <div className="flex items-center gap-2 text-xs mt-0.5" style={{ color: C.textSecondary }}>
              <span
                className="inline-block h-1.5 w-1.5 rounded-full shrink-0"
                style={{ background: live.reachable ? STATUS.online : STATUS.error }}
              />
              {live.reachable ? (
                <>
                  <span className="truncate" title={live.served_model ?? undefined}>
                    Engine serves: {live.served_model ?? "—"}
                  </span>
                  {live.drift && (
                    <span
                      className="rounded px-1.5 py-0.5 text-[10px] font-medium shrink-0"
                      style={{ color: STATUS_TEXT.warning, border: `1px solid ${STATUS.warning}` }}
                      title={`Registry says ${runtime.model_identifier ?? "—"} — will sync on the next watcher tick`}
                    >
                      Drift
                    </span>
                  )}
                </>
              ) : (
                <span style={{ color: STATUS_TEXT.error }}>
                  Engine unreachable ({live.consecutive_failures} probes)
                </span>
              )}
            </div>
          )}
        </div>

        </div>

        {/* Actions */}
        <div className="flex items-center gap-1.5 shrink-0 self-end sm:self-auto">
          {isLmStudio && (
            <button
              onClick={() => setSettingsOpen(v => !v)}
              title="Context settings"
              aria-label="Context settings"
              style={{
                padding: "4px",
                borderRadius: "6px",
                background: settingsOpen ? C.accentSubtle : "transparent",
                border: `1px solid ${settingsOpen ? C.borderAccent : "transparent"}`,
                color: settingsOpen ? C.accent : C.textMuted,
                cursor: "pointer",
                display: "flex",
                alignItems: "center",
                transition: "all 0.15s",
              }}
            >
              <Settings2 size={13} />
            </button>
          )}
          {isPowerManaged && (
            <ActionButton
              icon={Power}
              label="Wake"
              // Enabled when the box is asleep, or generally whenever it is not
              // yet serving (state !== "ready"); WoL is cheap and idempotent.
              disabled={(!isAsleep && effectiveState === "ready") || isMutating}
              onClick={() => wakeMutation.mutate()}
              loading={wakeMutation.isPending}
              variant="success"
            />
          )}
          <ActionButton
            icon={Play}
            label="Start"
            disabled={!canStart || isMutating}
            onClick={() => startMutation.mutate()}
            loading={startMutation.isPending}
            variant="success"
          />
          <ActionButton
            icon={Square}
            label="Stop"
            disabled={!canStop || isMutating}
            onClick={() => stopMutation.mutate()}
            loading={stopMutation.isPending}
            variant="danger"
          />
          {runtime.runtime_type !== "lmstudio" && (
            <ActionButton
              icon={RotateCcw}
              label="Restart"
              disabled={!canStop || isMutating}
              onClick={() => restartMutation.mutate()}
              loading={restartMutation.isPending}
              variant="default"
            />
          )}
          {isProbeable && (
            <ActionButton
              icon={RefreshCw}
              label="Re-probe model"
              disabled={isMutating}
              onClick={() => probeMutation.mutate()}
              loading={probeMutation.isPending}
              variant="default"
            />
          )}
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

      {/* Bound Agents Footer (Phase 15 T3.3) */}
      <BoundAgentsFooter runtime={runtime} />

    </motion.div>
  );
}

// ── KV Reset Schedule Toggle ──────────────────────────────────────────────────

function KvResetScheduleToggle() {
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
    onError: () => setResetMsg("KV Reset failed."),
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
        title="KV Reset Schedule"
      >
        ⏱ KV Reset
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
              <span className="text-xs font-medium" style={{ color: C.warning }}>KV Reset Schedule</span>
              <span className="text-xs" style={{ color: C.textMuted }}>
                — remembers active models, unloads all, reloads them
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
              ) : "⚡"}
              Run now
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


// ── Main Page ─────────────────────────────────────────────────────────────────

export default function RuntimesPage() {
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
            <h1
              className="text-xl font-semibold"
              style={{ color: C.textPrimary }}
            >
              Runtimes
            </h1>
            <p
              className="text-sm mt-0.5"
              style={{ color: C.textMuted }}
            >
              AI model runtimes and their hosts
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
              Add runtime
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
              Refresh
            </button>
          </div>
        </div>

        {/* Host metrics — one bar per enabled host (ADR-048) */}
        <HostMetricsBar />

        {/* vLLM Docker section */}
        <div className="mb-8">
          <div className="flex items-center gap-3 mb-4">
            <div className="w-px" style={{ alignSelf: "stretch", background: `linear-gradient(to bottom, ${C.info} 0%, transparent 100%)`, minHeight: "36px" }} />
            <div>
              <div className="flex items-center gap-2">
                <h2 className="text-sm font-semibold" style={{ color: C.textPrimary }}>vLLM Docker</h2>
                <span className="text-xs px-1.5 py-px rounded" style={{ color: C.textMuted, background: C.border, fontSize: "10px", letterSpacing: "0.06em" }}>Container</span>
              </div>
              <p className="text-xs mt-0.5" style={{ color: C.textMuted }}>Containerized models on GPU hosts</p>
            </div>
          </div>

          {isLoading && (
            <div className="flex items-center gap-2 py-2" style={{ color: C.textMuted }}>
              <Loader2 size={13} className="animate-spin" />
              <span className="text-xs">Loading runtimes...</span>
            </div>
          )}

          {error && (
            <div className="flex items-center gap-2 text-xs px-4 py-3 rounded-xl" style={{ color: STATUS_TEXT.error, background: `${C.error}0F`, border: `1px solid ${C.error}26` }}>
              <AlertCircle size={13} />
              Failed to load runtimes.
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
                  No vLLM Docker runtimes configured.
                </div>
              )}
            </div>
          )}
        </div>

        {/* LM Studio section */}
        <div>
          <div className="flex items-center gap-3 mb-4">
            <div className="w-px" style={{ alignSelf: "stretch", background: `linear-gradient(to bottom, ${C.accent} 0%, transparent 100%)`, minHeight: "36px" }} />
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <h2 className="text-sm font-semibold" style={{ color: C.textPrimary }}>LM Studio</h2>
                <span className="text-xs px-1.5 py-px rounded" style={{ color: C.textMuted, background: C.border, fontSize: "10px", letterSpacing: "0.06em" }}>LLM</span>
              </div>
              <p className="text-xs mt-0.5" style={{ color: C.textMuted }}>Locally installed models on DGX Spark</p>
            </div>
            <KvResetScheduleToggle />
          </div>

          <ModelCatalog />

          <ActiveDownloads />

          {!lmsData && lmsRuntimes.length === 0 && (
            <div className="flex items-center gap-2 py-2" style={{ color: C.textMuted }}>
              <Loader2 size={13} className="animate-spin" />
              <span className="text-xs">Connecting to DGX Spark...</span>
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
                      <span className="text-xs font-medium tracking-wider uppercase" style={{ color: C.online, letterSpacing: "0.07em", fontSize: "10px" }}>Active</span>
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
                      <span className="text-xs font-medium tracking-wider uppercase" style={{ color: C.textMuted, letterSpacing: "0.07em", fontSize: "10px" }}>Inactive</span>
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
        </div>

        {/* Hosts Registry (ADR-048) */}
        <HostsSection />

        {/* CLI-Tools (festgebackene Agent-Werkzeuge) */}
        <CliToolsSection />
      </div>

      <AddRuntimeModal open={addOpen} onClose={() => setAddOpen(false)} />
    </AppShell>
  );
}

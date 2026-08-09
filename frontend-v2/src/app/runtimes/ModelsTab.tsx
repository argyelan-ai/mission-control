"use client";

import { useState } from "react";
import { motion } from "framer-motion";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Download, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import type {
  LMStudioModel,
  LMSCatalogModel,
  HFRepoInfo,
  LMStudioModelsResponse,
} from "@/lib/types";
import { C, STATUS_TEXT } from "@/lib/colors";
import { VllmContainerCatalog } from "./VllmContainerCatalog";

// ── Active Downloads Panel ────────────────────────────────────────────────────
// Moved verbatim from the old page.tsx (~lines 131-225).

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

// ── Quantization Picker ─────────────────────────────────────────────────────
// Moved verbatim from the old page.tsx (~lines 583-642).

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
// Moved from the old page.tsx (~lines 646-973). The outer collapse header/button
// is gone — the Models tab itself is now the disclosure, so this always renders
// expanded; the LMS/HF tab toggle stays inside.

function ModelCatalog() {
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
      <div className="flex items-center gap-2 px-4 pt-3 pb-1 text-sm font-medium" style={{ color: C.textSecondary }}>
        <Download size={14} />
        Download model
      </div>

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
    </div>
  );
}

// ── Installed — not yet a runtime ─────────────────────────────────────────────
// New in this redesign: models LM Studio has on disk that have not been wired
// up as a Runtime row yet. One click adds them (old page.tsx `addRuntimeMutation`).

function UnattachedModelRow({ model }: { model: LMStudioModel }) {
  const queryClient = useQueryClient();

  const addRuntimeMutation = useMutation({
    mutationFn: () =>
      api.runtimes.addLmstudio({ lms_identifier: model.id, display_name: model.display_name }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["runtimes"] }),
  });

  return (
    <div
      className="flex items-center justify-between gap-3 px-3 py-2.5"
      style={{ background: C.borderSubtle, border: `1px solid ${C.borderSubtle}`, borderRadius: "10px" }}
    >
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium truncate" style={{ color: C.textPrimary }}>
          {model.display_name}
        </div>
        <div className="text-xs mt-0.5 tabular-nums" style={{ color: C.textMuted }}>
          {model.size_gb > 0 ? `${model.size_gb.toFixed(1)} GB` : "—"}
        </div>
      </div>
      <button
        onClick={() => addRuntimeMutation.mutate()}
        disabled={addRuntimeMutation.isPending}
        className="shrink-0 flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed transition-all"
        style={{
          background: C.accentSubtle,
          border: `1px solid ${C.borderAccent}`,
          color: C.accent,
        }}
      >
        {addRuntimeMutation.isPending && <Loader2 size={11} className="animate-spin" />}
        Add as runtime
      </button>
    </div>
  );
}

function UnattachedModelsSection() {
  const { data: lmsData } = useQuery<LMStudioModelsResponse>({
    queryKey: ["lms-models"],
    queryFn: api.lmstudio.list,
  });
  const { data: runtimesData } = useQuery({
    queryKey: ["runtimes"],
    queryFn: () => api.runtimes.list(),
  });

  const configuredLmsIds = new Set(
    (runtimesData?.runtimes ?? []).map((r) => r.lms_identifier).filter(Boolean)
  );
  const unattachedModels = (lmsData?.models ?? []).filter(
    (m) => !configuredLmsIds.has(m.id)
  );

  if (unattachedModels.length === 0) return null;

  return (
    <div className="mb-6">
      <div className="flex items-center gap-2 mb-2 px-0.5">
        <span className="text-xs font-medium tracking-wider uppercase" style={{ color: C.textMuted, letterSpacing: "0.07em", fontSize: "10px" }}>
          Installed — not yet a runtime
        </span>
        <div className="flex-1 h-px" style={{ background: C.border }} />
      </div>
      <div className="flex flex-col gap-2">
        {unattachedModels.map((model) => (
          <UnattachedModelRow key={model.id} model={model} />
        ))}
      </div>
    </div>
  );
}

// ── Models Tab ─────────────────────────────────────────────────────────────────

export function ModelsTab() {
  return (
    <div>
      <ModelCatalog />
      <ActiveDownloads />
      <UnattachedModelsSection />

      <div className="mb-3">
        <div className="flex items-center gap-2 mb-2 px-0.5">
          <span className="text-xs font-medium tracking-wider uppercase" style={{ color: C.textMuted, letterSpacing: "0.07em", fontSize: "10px" }}>
            Spark recipes
          </span>
          <div className="flex-1 h-px" style={{ background: C.border }} />
        </div>
        <VllmContainerCatalog />
      </div>
    </div>
  );
}

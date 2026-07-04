"use client";

/**
 * Guided add-runtime flow (ADR-053): paste a base URL → MC probes it live,
 * detects the engine type + served models → confirm name → runtime row
 * created. No model identifiers typed by hand.
 */

import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { X, Loader2, AlertCircle, CheckCircle2 } from "lucide-react";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { api } from "@/lib/api";
import { C, STATUS_TEXT } from "@/lib/colors";
import type { ProbeEndpointResult } from "@/lib/types";

const TYPE_LABEL: Record<string, string> = {
  vllm_docker: "vLLM",
  lmstudio: "LM Studio",
  openai_compatible: "OpenAI-compatible",
};

function slugify(name: string): string {
  return name
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

// Strip trailing slashes first, then ensure a single "/v1" suffix — avoids
// double-appending when the pasted URL already ends in "/v1/".
function normalizeEndpoint(url: string): string {
  const trimmed = url.replace(/\/+$/, "");
  return trimmed.endsWith("/v1") ? trimmed : `${trimmed}/v1`;
}

interface Props {
  open: boolean;
  onClose: () => void;
}

export function AddRuntimeModal({ open, onClose }: Props) {
  const queryClient = useQueryClient();
  const [url, setUrl] = useState("");
  const [probe, setProbe] = useState<ProbeEndpointResult | null>(null);
  const [model, setModel] = useState<string | null>(null);
  const [name, setName] = useState("");

  // iOS-safe scroll lock (matches BindAgentModal)
  useBodyScrollLock(open);

  const probeMutation = useMutation({
    mutationFn: () => api.runtimes.probeEndpoint(url),
    onSuccess: (res) => {
      setProbe(res);
      setModel(res.suggested_model);
      if (res.detected_type && !name) {
        let host = url;
        try {
          host = new URL(url).host;
        } catch {
          // leave raw url as fallback label source
        }
        setName(`${TYPE_LABEL[res.detected_type] ?? res.detected_type} @ ${host}`);
      }
    },
  });

  const createMutation = useMutation({
    mutationFn: () =>
      api.runtimes.db.create({
        slug: slugify(name),
        display_name: name,
        runtime_type: probe!.detected_type!,
        endpoint: normalizeEndpoint(url),
        model_identifier: model ?? undefined,
        enabled: true,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["runtimes"] });
      handleClose();
    },
  });

  function handleClose() {
    onClose();
    setUrl("");
    setProbe(null);
    setModel(null);
    setName("");
    probeMutation.reset();
    createMutation.reset();
  }

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15 }}
          className="fixed inset-0 z-40 flex items-end sm:items-center justify-center sm:p-4 bg-black/60"
          style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
          onClick={handleClose}
        >
          <div
            className="sm:hidden absolute bottom-[calc(92dvh-0.5rem)] left-1/2 -translate-x-1/2 z-10 w-8 h-1 rounded-full pointer-events-none"
            style={{ backgroundColor: "rgba(255,255,255,0.18)" }}
          />

          <motion.div
            initial={{ opacity: 0, y: 32 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 32 }}
            transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
            className="relative w-full mx-2 rounded-t-2xl rounded-b-none sm:mx-0 sm:max-w-md sm:rounded-2xl overflow-hidden max-h-[92dvh] sm:max-h-[88vh] flex flex-col"
            onClick={(e) => e.stopPropagation()}
            style={{
              backgroundColor: "var(--color-bg-elevated)",
              border: "1px solid rgba(255,255,255,0.08)",
              boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
            }}
          >
            <div
              className="flex items-center justify-between p-5 border-b shrink-0"
              style={{ borderColor: "rgba(255,255,255,0.06)" }}
            >
              <div>
                <h2 className="text-sm font-semibold" style={{ color: "var(--color-text-primary)" }}>
                  Add runtime
                </h2>
                <div className="text-[11px] mt-0.5" style={{ color: "var(--color-text-muted)" }}>
                  Paste a base URL — MC probes it and detects the engine + models.
                </div>
              </div>
              <button
                onClick={handleClose}
                className="p-1 rounded-md hover:bg-[rgba(255,255,255,0.06)] cursor-pointer"
              >
                <X size={14} style={{ color: "var(--color-text-muted)" }} />
              </button>
            </div>

            <div className="flex-1 overflow-y-auto p-5 space-y-4">
              {/* Step 1: URL + Probe */}
              <div>
                <label className="text-[11px] font-medium block mb-1.5" style={{ color: C.textMuted }}>
                  Endpoint URL
                </label>
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={url}
                    onChange={(e) => setUrl(e.target.value)}
                    placeholder="http://192.168.1.x:8000/v1"
                    className="flex-1 text-[13px] px-3 py-2 rounded-lg outline-none"
                    style={{
                      backgroundColor: C.bgSurface,
                      border: `1px solid ${C.border}`,
                      color: C.textPrimary,
                    }}
                  />
                  <button
                    onClick={() => probeMutation.mutate()}
                    disabled={!url.trim() || probeMutation.isPending}
                    className="flex items-center gap-1.5 text-xs px-3 py-2 rounded-lg transition-all cursor-pointer disabled:cursor-not-allowed disabled:opacity-50 shrink-0"
                    style={{
                      color: C.accent,
                      border: `1px solid ${C.borderAccent}`,
                      background: C.accentSubtle,
                    }}
                  >
                    {probeMutation.isPending ? (
                      <Loader2 size={12} className="animate-spin" />
                    ) : null}
                    Probe
                  </button>
                </div>
              </div>

              {/* Probe result — unreachable */}
              {probe && !probe.reachable && (
                <div
                  className="flex items-center gap-2 text-xs px-3 py-2.5 rounded-lg"
                  style={{ color: STATUS_TEXT.error, background: `${C.error}0F`, border: `1px solid ${C.error}26` }}
                >
                  <AlertCircle size={13} className="shrink-0" />
                  {probe.error ?? "Endpoint unreachable."}
                </div>
              )}

              {/* Probe result — reachable */}
              {probe && probe.reachable && (
                <>
                  <div className="flex items-center gap-2 text-xs px-3 py-2.5 rounded-lg" style={{ color: STATUS_TEXT.info, background: `${C.info}0F`, border: `1px solid ${C.info}26` }}>
                    <CheckCircle2 size={13} className="shrink-0" style={{ color: C.online }} />
                    <span>
                      Detected{" "}
                      <span className="font-semibold" style={{ color: C.textPrimary }}>
                        {probe.detected_type ? TYPE_LABEL[probe.detected_type] ?? probe.detected_type : "unknown"}
                      </span>
                      {" · "}
                      {probe.models.length} model{probe.models.length === 1 ? "" : "s"}
                    </span>
                  </div>

                  <div>
                    <label className="text-[11px] font-medium block mb-1.5" style={{ color: C.textMuted }}>
                      Model
                    </label>
                    <select
                      value={model ?? ""}
                      onChange={(e) => setModel(e.target.value)}
                      className="w-full text-[13px] px-3 py-2 rounded-lg outline-none"
                      style={{
                        backgroundColor: C.bgSurface,
                        border: `1px solid ${C.border}`,
                        color: C.textPrimary,
                      }}
                    >
                      {probe.models.map((m) => (
                        <option key={m} value={m}>
                          {m}
                        </option>
                      ))}
                    </select>
                  </div>

                  <div>
                    <label className="text-[11px] font-medium block mb-1.5" style={{ color: C.textMuted }}>
                      Display name
                    </label>
                    <input
                      type="text"
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      placeholder="Name this runtime"
                      className="w-full text-[13px] px-3 py-2 rounded-lg outline-none"
                      style={{
                        backgroundColor: C.bgSurface,
                        border: `1px solid ${C.border}`,
                        color: C.textPrimary,
                      }}
                    />
                  </div>

                  {createMutation.isError && (
                    <div
                      className="flex items-center gap-2 text-xs px-3 py-2.5 rounded-lg"
                      style={{ color: STATUS_TEXT.error, background: `${C.error}0F`, border: `1px solid ${C.error}26` }}
                    >
                      <AlertCircle size={13} className="shrink-0" />
                      {createMutation.error instanceof Error ? createMutation.error.message : "Failed to create runtime."}
                    </div>
                  )}

                  <button
                    onClick={() => createMutation.mutate()}
                    disabled={!name.trim() || slugify(name) === "" || !model || createMutation.isPending}
                    className="w-full flex items-center justify-center gap-1.5 text-xs px-3 py-2.5 rounded-lg transition-all cursor-pointer disabled:cursor-not-allowed disabled:opacity-50"
                    style={{
                      color: C.accent,
                      border: `1px solid ${C.borderAccent}`,
                      background: C.accentSubtle,
                    }}
                  >
                    {createMutation.isPending ? <Loader2 size={12} className="animate-spin" /> : null}
                    Add runtime
                  </button>
                </>
              )}
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

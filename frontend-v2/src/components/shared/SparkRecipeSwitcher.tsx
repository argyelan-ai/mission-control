"use client";

import { useState, useRef, useEffect } from "react";
import { createPortal } from "react-dom";
import { motion, AnimatePresence } from "framer-motion";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, ChevronDown, Check, X, AlertCircle } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";

/**
 * SparkRecipeSwitcher — surfaces sparkrun recipes for a vllm_docker runtime.
 *
 * Pattern:
 * - Mounted when the parent decides the runtime *might* be sparkrun-managed
 *   (vllm_docker type). We let the backend tell us for sure via
 *   ``/current-recipe`` and render a disabled "Not sparkrun-managed" hint
 *   instead of guessing client-side.
 * - Recipe list is fetched lazily (only when the user opens the dropdown)
 *   so we don't SSH-storm the Spark host on every runtimes-page render.
 * - Switching is a single mutation; the UI shows a warmup-notice on success
 *   because the actual model load takes ~5 min on the Spark side.
 */
export function SparkRecipeSwitcher({
  runtimeId,
  onSwitched,
}: {
  runtimeId: string;
  onSwitched?: () => void;
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [confirmRecipe, setConfirmRecipe] = useState<string | null>(null);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [coords, setCoords] = useState<{
    top: number;
    right: number;
    dropUp: boolean;
  } | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);

  // Compute viewport-relative coords for the portal-rendered dropdown.
  // Rendering into document.body (not the runtime card) lets us escape
  // the parent's overflow + stacking context, so the list never gets
  // clipped no matter where the card sits on the page.
  useEffect(() => {
    if (!open || !buttonRef.current) {
      setCoords(null);
      return;
    }
    const update = () => {
      if (!buttonRef.current) return;
      const rect = buttonRef.current.getBoundingClientRect();
      const spaceBelow = window.innerHeight - rect.bottom;
      const spaceAbove = rect.top;
      const dropUp = spaceBelow < 420 && spaceAbove > spaceBelow;
      setCoords({
        top: dropUp ? rect.top - 4 : rect.bottom + 4,
        right: window.innerWidth - rect.right,
        dropUp,
      });
    };
    update();
    window.addEventListener("scroll", update, true);
    window.addEventListener("resize", update);
    return () => {
      window.removeEventListener("scroll", update, true);
      window.removeEventListener("resize", update);
    };
  }, [open]);

  const currentQuery = useQuery({
    queryKey: ["runtime-current-recipe", runtimeId],
    queryFn: () => api.runtimes.sparkrun.currentRecipe(runtimeId),
  });

  const listQuery = useQuery({
    queryKey: ["sparkrun-recipes"],
    queryFn: () => api.runtimes.sparkrun.listRecipes(),
    enabled: open, // lazy — only fetch when dropdown is opened
  });

  const switchMutation = useMutation({
    mutationFn: (recipe: string) =>
      api.runtimes.sparkrun.switchRecipe(runtimeId, recipe),
    onSuccess: (data) => {
      setStatusMsg(data.message);
      setConfirmRecipe(null);
      setOpen(false);
      queryClient.invalidateQueries({ queryKey: ["runtime-current-recipe", runtimeId] });
      queryClient.invalidateQueries({ queryKey: ["runtimes"] });
      onSwitched?.();
    },
    onError: (err: Error) => {
      setStatusMsg(`Switch failed: ${err.message}`);
      setConfirmRecipe(null);
    },
  });

  const isSparkrun = currentQuery.data?.sparkrun_managed ?? false;
  const currentRecipe = currentQuery.data?.current_recipe;

  if (currentQuery.isLoading) {
    return (
      <span className="text-xs" style={{ color: "var(--color-text-muted)" }}>
        <Loader2 size={11} className="inline animate-spin" />
      </span>
    );
  }

  if (!isSparkrun) {
    return null; // silently absent — the parent shows nothing
  }

  return (
    <div className="relative">
      <button
        ref={buttonRef}
        onClick={() => setOpen((v) => !v)}
        disabled={switchMutation.isPending}
        title={`Active recipe: ${currentRecipe ?? "—"}`}
        style={{
          display: "flex",
          alignItems: "center",
          gap: "4px",
          padding: "3px 8px",
          borderRadius: "6px",
          background: open ? `${C.accent}26` : "rgba(255,255,255,0.04)",
          border: `1px solid ${open ? `${C.accent}4D` : "var(--color-border-subtle)"}`,
          color: open ? C.accentHover : "var(--color-text-secondary)",
          fontSize: "11px",
          cursor: switchMutation.isPending ? "not-allowed" : "pointer",
          transition: "all 0.15s",
          maxWidth: "240px",
        }}
      >
        <span className="truncate font-mono text-[10px]">
          {currentRecipe ?? "no recipe"}
        </span>
        {switchMutation.isPending ? (
          <Loader2 size={11} className="animate-spin shrink-0" />
        ) : (
          <ChevronDown size={11} className="shrink-0" />
        )}
      </button>

      {typeof document !== "undefined" && createPortal(
        <AnimatePresence>
          {open && coords && (
            <motion.div
              initial={{ opacity: 0, y: coords.dropUp ? 4 : -4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: coords.dropUp ? 4 : -4 }}
              transition={{ duration: 0.15 }}
              style={{
                position: "fixed",
                ...(coords.dropUp
                  ? { bottom: window.innerHeight - coords.top }
                  : { top: coords.top }),
                right: coords.right,
                minWidth: "320px",
                maxWidth: "420px",
                maxHeight: "400px",
                overflowY: "auto",
                background: "var(--color-bg-elevated)",
                border: "1px solid var(--color-border-subtle)",
                borderRadius: "8px",
                boxShadow: "0 8px 24px rgba(0,0,0,0.4)",
                zIndex: 1000,
                padding: "6px",
              }}
            >
            {listQuery.isLoading && (
              <div
                className="flex items-center gap-2 px-3 py-2 text-xs"
                style={{ color: "var(--color-text-muted)" }}
              >
                <Loader2 size={12} className="animate-spin" /> Loading recipes via SSH…
              </div>
            )}
            {listQuery.isError && (
              <div
                className="flex items-center gap-2 px-3 py-2 text-xs"
                style={{ color: C.error }}
              >
                <AlertCircle size={12} /> Could not reach sparkrun on the Spark host.
              </div>
            )}
            {listQuery.data?.recipes.length === 0 && (
              <div
                className="px-3 py-2 text-xs"
                style={{ color: "var(--color-text-muted)" }}
              >
                No recipes returned by sparkrun list.
              </div>
            )}
            {listQuery.data?.recipes.map((r) => {
              const isActive = r.name === currentRecipe;
              const isConfirm = confirmRecipe === r.name;
              const isDisabled = !r.solo_capable;
              const gpuHint =
                r.tp != null || r.nodes != null
                  ? `tp=${r.tp ?? 1}${r.nodes != null ? `, nodes=${r.nodes}` : ""}`
                  : null;
              return (
                <div
                  key={r.name}
                  title={
                    isDisabled
                      ? `Braucht ${gpuHint ?? "mehr GPUs/Nodes"} — nicht solo-startbar auf diesem Host`
                      : undefined
                  }
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    padding: "6px 8px",
                    borderRadius: "6px",
                    background: isActive
                      ? "rgba(74,222,128,0.08)"
                      : isConfirm
                      ? "rgba(255,178,36,0.08)"
                      : "transparent",
                    opacity: isDisabled ? 0.45 : 1,
                    cursor: isActive || isDisabled ? "default" : "pointer",
                    transition: "background 0.12s",
                  }}
                  onClick={() => {
                    if (!isActive && !isDisabled && !switchMutation.isPending) {
                      setConfirmRecipe(isConfirm ? null : r.name);
                    }
                  }}
                  onMouseEnter={(e) => {
                    if (!isActive && !isConfirm && !isDisabled) {
                      (e.currentTarget as HTMLDivElement).style.background =
                        "rgba(255,255,255,0.04)";
                    }
                  }}
                  onMouseLeave={(e) => {
                    if (!isActive && !isConfirm && !isDisabled) {
                      (e.currentTarget as HTMLDivElement).style.background = "transparent";
                    }
                  }}
                >
                  <div className="flex items-center gap-2">
                    {isActive && <Check size={11} style={{ color: C.online, flexShrink: 0 }} />}
                    {isDisabled && (
                      <AlertCircle size={11} style={{ color: C.warning, flexShrink: 0 }} />
                    )}
                    <span
                      className="font-mono text-[11px] truncate"
                      style={{ color: "var(--color-text-primary)", flex: 1 }}
                    >
                      {r.name}
                    </span>
                    {gpuHint && (
                      <span
                        className="text-[9px] font-mono px-1.5 py-0.5 rounded shrink-0"
                        style={{
                          background: "rgba(255,255,255,0.05)",
                          color: "var(--color-text-muted)",
                        }}
                      >
                        {gpuHint}
                      </span>
                    )}
                    <span
                      className="text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded shrink-0"
                      style={{
                        background:
                          r.registry === "official"
                            ? `${C.accent}26`
                            : "rgba(255,255,255,0.05)",
                        color:
                          r.registry === "official" ? C.accentHover : "var(--color-text-muted)",
                      }}
                    >
                      {r.registry}
                    </span>
                  </div>
                  {r.model && (
                    <span
                      className="text-[10px] truncate font-mono"
                      style={{ color: "var(--color-text-muted)", marginTop: "2px" }}
                    >
                      {r.model}
                    </span>
                  )}
                  {isDisabled && (
                    <span
                      className="text-[10px]"
                      style={{ color: C.warning, marginTop: "2px" }}
                    >
                      Braucht {gpuHint ?? "mehr GPUs/Nodes"} — nicht solo-startbar
                    </span>
                  )}
                  {isConfirm && (
                    <div className="flex items-center gap-2 mt-2">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          switchMutation.mutate(r.name);
                        }}
                        style={{
                          padding: "3px 8px",
                          borderRadius: "4px",
                          background: C.accent,
                          color: C.textPrimary,
                          fontSize: "10px",
                          fontWeight: 600,
                          border: "none",
                          cursor: "pointer",
                        }}
                      >
                        Confirm switch
                      </button>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          setConfirmRecipe(null);
                        }}
                        style={{
                          padding: "3px 6px",
                          borderRadius: "4px",
                          background: "transparent",
                          color: "var(--color-text-muted)",
                          fontSize: "10px",
                          border: "1px solid var(--color-border-subtle)",
                          cursor: "pointer",
                          display: "flex",
                          alignItems: "center",
                          gap: "3px",
                        }}
                      >
                        <X size={10} /> Cancel
                      </button>
                      <span
                        className="text-[10px]"
                        style={{ color: "var(--color-text-muted)" }}
                      >
                        Warmup ~5 min after switch.
                      </span>
                    </div>
                  )}
                </div>
              );
            })}
            </motion.div>
          )}
        </AnimatePresence>,
        document.body,
      )}

      {statusMsg && !open && (
        <div
          className="absolute top-full right-0 mt-1 text-xs px-2 py-1 rounded"
          style={{
            background: "var(--color-bg-elevated)",
            border: "1px solid var(--color-border-subtle)",
            color: "var(--color-text-secondary)",
            maxWidth: "300px",
            zIndex: 40,
          }}
        >
          {statusMsg}
          <button
            onClick={() => setStatusMsg(null)}
            style={{
              marginLeft: "6px",
              color: "var(--color-text-muted)",
              cursor: "pointer",
              border: "none",
              background: "transparent",
            }}
          >
            ×
          </button>
        </div>
      )}
    </div>
  );
}

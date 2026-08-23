"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { motion } from "framer-motion";
import { C, STATUS_TEXT } from "@/lib/colors";
import { fmtCtx } from "@/lib/utils";

// ── Context Presets ───────────────────────────────────────────────────────────
// Verbatim copy of page.tsx's context-settings block (CTX_PRESETS,
// CTX_STORAGE_KEY, loadStoredCtx, saveStoredCtx, ContextSettingsPanel) —
// extracted so RuntimeDetailPanel can reuse it without importing page.tsx.

export const CTX_PRESETS = [4096, 8192, 16384, 32768, 65536, 131072, 200000, 262144];

export const CTX_STORAGE_KEY = (modelId: string) => `lms-ctx-${modelId}`;

export function loadStoredCtx(modelId: string): number | null {
  try {
    const v = localStorage.getItem(CTX_STORAGE_KEY(modelId));
    return v ? parseInt(v, 10) : null;
  } catch { return null; }
}

export function saveStoredCtx(modelId: string, ctx: number | null) {
  try {
    if (ctx === null) localStorage.removeItem(CTX_STORAGE_KEY(modelId));
    else localStorage.setItem(CTX_STORAGE_KEY(modelId), String(ctx));
  } catch {}
}

// ── Context Settings Panel ────────────────────────────────────────────────────

export function ContextSettingsPanel({
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
              className="flex-1 text-xs font-mono px-2 py-1 rounded-sm disabled:opacity-30"
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

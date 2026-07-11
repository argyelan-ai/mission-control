"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { notify } from "@/lib/notify";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { benchApi } from "@/verticals/bench_studio/api";
import type { BenchModelSpec, PromptTemplate } from "./types";

const EMPTY_MODEL: BenchModelSpec = { label: "", source_kind: "spark", spark_model: "" };

export function NewChallengeDialog({
  open,
  onClose,
  prefillTemplate,
}: {
  open: boolean;
  onClose: () => void;
  prefillTemplate: PromptTemplate | null;
}) {
  const qc = useQueryClient();
  const [title, setTitle] = useState("");
  const [promptText, setPromptText] = useState("");
  const [templateId, setTemplateId] = useState<string | null>(null);
  const [mode, setMode] = useState<"single" | "side_by_side">("side_by_side");
  const [seriesLabel, setSeriesLabel] = useState("");
  const [models, setModels] = useState<BenchModelSpec[]>([{ ...EMPTY_MODEL }]);

  const { data: agents } = useQuery({
    queryKey: ["agents-for-bench"],
    queryFn: () => api.agents.list(),
    enabled: open,
  });

  const { data: templates } = useQuery({
    queryKey: ["prompt-templates-for-bench"],
    queryFn: benchApi.promptTemplates.list,
    enabled: open,
  });

  // Prefill from the Prompt Library ("Challenge starten") — also preselects the dropdown:
  useEffect(() => {
    if (prefillTemplate && open) {
      setTitle(prefillTemplate.title);
      setPromptText(prefillTemplate.body);
      setTemplateId(prefillTemplate.id);
    }
  }, [prefillTemplate, open]);

  const mutation = useMutation({
    mutationFn: () =>
      benchApi.challenges.create({
        title,
        prompt_template_id: templateId,
        prompt_text: promptText,
        mode,
        models,
        series_label: seriesLabel.trim() || null,
      }),
    onSuccess: () => {
      notify.success("Challenge gestartet");
      qc.invalidateQueries({ queryKey: ["bench-challenges"] });
      reset();
      onClose();
    },
    onError: () => notify.error("Challenge konnte nicht gestartet werden"),
  });

  function reset() {
    setTitle("");
    setPromptText("");
    setTemplateId(null);
    setMode("side_by_side");
    setSeriesLabel("");
    setModels([{ ...EMPTY_MODEL }]);
  }

  function handleTemplateSelect(id: string) {
    if (!id) {
      // Switching back to Freitext — clear template id, keep text editable
      setTemplateId(null);
      return;
    }
    const tpl = (templates ?? []).find((t) => t.id === id);
    if (tpl) {
      setTemplateId(tpl.id);
      setPromptText(tpl.body);
    }
  }

  function setModel(i: number, patch: Partial<BenchModelSpec>) {
    setModels((prev) => prev.map((m, idx) => (idx === i ? { ...m, ...patch } : m)));
  }

  const valid =
    title.trim().length > 0 &&
    (templateId !== null || promptText.trim().length > 0) &&
    models.length > 0 &&
    models.every(
      (m) =>
        m.label.trim().length > 0 &&
        (m.source_kind === "spark" || (m.source_kind === "agent" && m.agent_id))
    );

  const inputStyle = {
    backgroundColor: C.bgDeep,
    color: C.textPrimary,
    border: `1px solid ${C.border}`,
  } as const;

  return (
    <ResponsiveModal open={open} onClose={onClose} aria-label="Neue Challenge">
      <div
        className="flex flex-col gap-4 p-5 rounded-xl w-full max-h-[85vh] overflow-y-auto"
        style={{ backgroundColor: C.bgElevated, border: `1px solid ${C.border}` }}
      >
        <h3 className="text-base font-semibold" style={{ color: C.textPrimary }}>
          Neue Challenge
        </h3>

        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Titel"
          className="rounded-lg p-2.5 text-sm outline-none"
          style={inputStyle}
        />

        {/* Template picker: "Freitext" default + one option per template */}
        <select
          value={templateId ?? ""}
          onChange={(e) => handleTemplateSelect(e.target.value)}
          className="rounded-lg p-2.5 text-sm outline-none"
          style={inputStyle}
          aria-label="Template wählen"
        >
          <option value="">Freitext</option>
          {(templates ?? []).map((tpl) => (
            <option key={tpl.id} value={tpl.id}>
              {tpl.title}
            </option>
          ))}
        </select>

        <textarea
          value={promptText}
          onChange={(e) => {
            setPromptText(e.target.value);
            // Don't clear templateId — edited text + template ID both win (backend uses text if provided)
          }}
          rows={5}
          placeholder="Prompt (oder Template oben wählen)"
          className="rounded-lg p-3 text-sm resize-none outline-none"
          style={inputStyle}
        />
        {templateId && (
          <span className="text-xs" style={{ color: C.textMuted }}>
            Aus Template — Kopie wird beim Start eingefroren.
          </span>
        )}

        <div className="flex gap-3">
          <select
            value={mode}
            onChange={(e) => setMode(e.target.value as "single" | "side_by_side")}
            className="rounded-lg p-2.5 text-sm outline-none flex-1"
            style={inputStyle}
            aria-label="Modus"
          >
            <option value="side_by_side">Side-by-Side</option>
            <option value="single">Single</option>
          </select>
          <input
            value={seriesLabel}
            onChange={(e) => setSeriesLabel(e.target.value)}
            placeholder="Serien-Label (optional)"
            className="rounded-lg p-2.5 text-sm outline-none flex-1"
            style={inputStyle}
          />
        </div>

        <div className="flex flex-col gap-2">
          <span className="text-sm font-medium" style={{ color: C.textSecondary }}>
            Modelle
          </span>
          {models.map((m, i) => (
            <div key={i} className="flex gap-2 items-center">
              <input
                value={m.label}
                onChange={(e) => setModel(i, { label: e.target.value })}
                placeholder="Label (z. B. DeepSeek)"
                className="rounded-lg p-2 text-sm outline-none flex-1"
                style={inputStyle}
              />
              <select
                value={m.source_kind}
                onChange={(e) =>
                  setModel(i, {
                    source_kind: e.target.value as "spark" | "agent",
                    spark_model: "",
                    agent_id: null,
                  })
                }
                className="rounded-lg p-2 text-sm outline-none"
                style={inputStyle}
                aria-label={`Quelle ${i + 1}`}
              >
                <option value="spark">Spark</option>
                <option value="agent">Agent</option>
              </select>
              {m.source_kind === "spark" ? (
                <input
                  value={m.spark_model ?? ""}
                  onChange={(e) => setModel(i, { spark_model: e.target.value || null })}
                  placeholder="vLLM-Modell (leer = aktiv)"
                  className="rounded-lg p-2 text-sm outline-none flex-1"
                  style={inputStyle}
                />
              ) : (
                <select
                  value={m.agent_id ?? ""}
                  onChange={(e) => setModel(i, { agent_id: e.target.value || null })}
                  className="rounded-lg p-2 text-sm outline-none flex-1"
                  style={inputStyle}
                  aria-label={`Agent ${i + 1}`}
                >
                  <option value="">Agent wählen …</option>
                  {(agents ?? []).map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name}
                    </option>
                  ))}
                </select>
              )}
              <button
                onClick={() => setModels((prev) => prev.filter((_, idx) => idx !== i))}
                disabled={models.length <= 1}
                aria-label={`Modell ${i + 1} entfernen`}
                className="disabled:opacity-30"
                style={{ color: C.textMuted }}
              >
                <Trash2 size={14} />
              </button>
            </div>
          ))}
          <button
            onClick={() => setModels((prev) => [...prev, { ...EMPTY_MODEL }])}
            disabled={models.length >= 6}
            className="self-start flex items-center gap-1 text-xs disabled:opacity-40"
            style={{ color: C.accent }}
          >
            <Plus size={12} /> Modell hinzufügen
          </button>
        </div>

        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="px-3 py-1.5 rounded-lg text-sm"
            style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
          >
            Abbrechen
          </button>
          <button
            onClick={() => mutation.mutate()}
            disabled={!valid || mutation.isPending}
            className="px-3 py-1.5 rounded-lg text-sm font-medium disabled:opacity-40"
            style={{ backgroundColor: C.accentSubtle, color: C.accent, border: `1px solid ${C.borderAccent}` }}
          >
            Challenge starten
          </button>
        </div>
      </div>
    </ResponsiveModal>
  );
}

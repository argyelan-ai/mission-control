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

const EMPTY_MODEL: BenchModelSpec = { label: "", source_kind: "spark", spark_model: "", display_tag: "" };

/** request() throws `Error("API <status>: <raw body>")` — pull the backend's
 *  `detail` string back out (e.g. the 422 "Spark nicht erreichbar…") so the
 *  toast shows the real reason instead of a generic one. Mirrors
 *  ChallengeDetail.tsx's apiErrorDetail. */
function apiErrorDetail(err: unknown, fallback: string): string {
  if (err instanceof Error) {
    const match = err.message.match(/^API \d+: ([\s\S]*)$/);
    if (match) {
      try {
        const body = JSON.parse(match[1]);
        if (typeof body?.detail === "string" && body.detail) return body.detail;
      } catch {
        // body wasn't JSON — fall through to fallback
      }
    }
  }
  return fallback;
}

const RECORD_DURATION_MIN = 5;
const RECORD_DURATION_MAX = 60;
const RECORD_DURATION_DEFAULT = 20;

// Empty/non-numeric input (e.g. the field cleared mid-edit) falls back to
// the default rather than left in an invalid state — the backend's 5..60
// bound (422 outside it) should never be reachable from this field.
function clampRecordDuration(raw: string): number {
  if (raw.trim() === "") return RECORD_DURATION_DEFAULT;
  const n = Number(raw);
  if (Number.isNaN(n)) return RECORD_DURATION_DEFAULT;
  return Math.min(RECORD_DURATION_MAX, Math.max(RECORD_DURATION_MIN, Math.round(n)));
}

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
  // Raw typed string, not the clamped number — clamping the displayed value
  // on every keystroke corrupts multi-digit entry (typing "45" digit by
  // digit would clamp the intermediate "4" to 5, then append the next "5"
  // onto THAT, landing on 55). recordDurationS below is the clamped value
  // actually used for the payload/valid-check; the input re-normalizes to
  // it on blur.
  const [recordDurationRaw, setRecordDurationRaw] = useState(String(RECORD_DURATION_DEFAULT));
  const recordDurationS = clampRecordDuration(recordDurationRaw);
  const [models, setModels] = useState<BenchModelSpec[]>([{ ...EMPTY_MODEL }]);
  // Row indices where the label was edited by hand — autofill must not overwrite those.
  const [labelTouched, setLabelTouched] = useState<Set<number>>(new Set());

  const { data: agents } = useQuery({
    queryKey: ["agents-for-bench"],
    queryFn: () => api.agents.list(),
    enabled: open,
  });

  // Bench #21: live model list for the vanilla (direct-API) row's select.
  const { data: sparkModels, isError: sparkModelsErrored } = useQuery({
    queryKey: ["bench-spark-models"],
    queryFn: benchApi.sparkModels.get,
    enabled: open,
    staleTime: 30_000,
  });
  // Treat a query ERROR the same as an explicit reachable:false — otherwise
  // a network/5xx failure leaves sparkModels undefined and the dialog falls
  // through to the reachable-select branch with an empty model list and no
  // warning (review finding NIT 1).
  const sparkOffline = sparkModelsErrored || sparkModels?.reachable === false;

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
        models: models.map((m) => ({
          ...m,
          display_tag: m.display_tag?.trim() || null,
        })),
        series_label: seriesLabel.trim() || null,
        record_duration_s: recordDurationS,
      }),
    onSuccess: () => {
      notify.success("Challenge gestartet");
      qc.invalidateQueries({ queryKey: ["bench-challenges"] });
      reset();
      onClose();
    },
    onError: (err) => notify.error(apiErrorDetail(err, "Challenge konnte nicht gestartet werden")),
  });

  function reset() {
    setTitle("");
    setPromptText("");
    setTemplateId(null);
    setMode("side_by_side");
    setSeriesLabel("");
    setRecordDurationRaw(String(RECORD_DURATION_DEFAULT));
    setModels([{ ...EMPTY_MODEL }]);
    setLabelTouched(new Set());
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

  // Autofill: übernimmt den gewählten Modell-/Agentennamen ins Label,
  // solange Mark das Label der Zeile nicht selbst angefasst hat.
  function setModelWithAutofill(i: number, patch: Partial<BenchModelSpec>) {
    setModels((prev) =>
      prev.map((m, idx) => {
        if (idx !== i) return m;
        const next = { ...m, ...patch };
        if (!labelTouched.has(i)) {
          if (next.source_kind === "spark") {
            // Mirrors an EXPLICITLY selected model only. The "Aktives
            // Modell (auto)" case (spark_model "") deliberately does NOT
            // fill the label here — the resolved model is only shown as a
            // placeholder (see the label input below); writing it into the
            // actual value would go stale the moment the box's active
            // model changes between dialog-open and create (review finding
            // MINOR 1). The backend fills the label from the model it
            // actually resolves at create time when left blank.
            next.label = (next.spark_model ?? "").trim();
          } else if (next.agent_id) {
            const agent = (agents ?? []).find((a) => a.id === next.agent_id);
            next.label = (agent?.model ?? agent?.name ?? "").trim();
          } else {
            next.label = "";
          }
        }
        return next;
      })
    );
  }

  function markLabelTouched(i: number, value: string) {
    setLabelTouched((prev) => {
      const next = new Set(prev);
      // Leeres Feld gilt wieder als unberührt → Autofill greift erneut.
      if (value.trim()) next.add(i);
      else next.delete(i);
      return next;
    });
  }

  // Derived chip-tag default — mirrors the backend fallback
  // (bench_studio/orchestrator._build_branding_payload): spark -> vLLM,
  // agent -> harness uppercased, agent name as last resort.
  function derivedTag(m: BenchModelSpec): string {
    if (m.source_kind === "spark") return "VLLM · SPARK";
    const agent = (agents ?? []).find((a) => a.id === m.agent_id);
    return (agent?.harness ?? agent?.name ?? "AGENT").toUpperCase();
  }

  // A spark row still on "Aktives Modell (auto)" (spark_model empty/null) —
  // the backend resolves + freezes the live model for these at create time.
  function isAutoSparkRow(m: BenchModelSpec): boolean {
    return m.source_kind === "spark" && !(m.spark_model ?? "").trim();
  }

  // Review finding MINOR 1 / NIT 2: an untouched auto row is valid with an
  // empty label (the backend fills it in) — UNLESS Spark is confirmed
  // offline, in which case create() would 422 immediately regardless of the
  // label, so submit stays blocked until the operator either types a model
  // (free-text fallback) or Spark comes back.
  function isModelRowValid(m: BenchModelSpec, i: number): boolean {
    if (m.source_kind === "agent") {
      return m.label.trim().length > 0 && Boolean(m.agent_id);
    }
    if (isAutoSparkRow(m)) {
      if (sparkOffline) return false;
      return !labelTouched.has(i) || m.label.trim().length > 0;
    }
    return m.label.trim().length > 0;
  }

  const valid =
    title.trim().length > 0 &&
    (templateId !== null || promptText.trim().length > 0) &&
    models.length > 0 &&
    models.every((m, i) => isModelRowValid(m, i)) &&
    // Belt-and-suspenders: onChange already clamps to 5..60, but guarding
    // the submit too means an out-of-range value disables the button
    // instead of surfacing as a generic 422 from the backend.
    recordDurationS >= RECORD_DURATION_MIN &&
    recordDurationS <= RECORD_DURATION_MAX;

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

        <div className="flex gap-3 items-end">
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
          <div className="flex flex-col gap-1">
            <label htmlFor="bench-record-duration" className="text-xs" style={{ color: C.textMuted }}>
              Video-Länge (s)
            </label>
            <input
              id="bench-record-duration"
              type="number"
              min={RECORD_DURATION_MIN}
              max={RECORD_DURATION_MAX}
              step={1}
              value={recordDurationRaw}
              onChange={(e) => setRecordDurationRaw(e.target.value)}
              onBlur={() => setRecordDurationRaw(String(clampRecordDuration(recordDurationRaw)))}
              className="rounded-lg p-2.5 text-sm outline-none w-24"
              style={inputStyle}
            />
          </div>
        </div>

        <div className="flex flex-col gap-2">
          <span className="text-sm font-medium" style={{ color: C.textSecondary }}>
            Modelle
          </span>
          {models.map((m, i) => (
            <div key={i} className="flex gap-2 items-center">
              <input
                value={m.label}
                onChange={(e) => {
                  markLabelTouched(i, e.target.value);
                  setModel(i, { label: e.target.value });
                }}
                placeholder={
                  // Untouched auto row: preview the model the backend will
                  // resolve at create time — as a placeholder only, never
                  // written into the submitted value (MINOR 1).
                  isAutoSparkRow(m) && !sparkOffline && sparkModels?.active
                    ? `Label (auto: ${sparkModels.active})`
                    : "Label (z. B. DeepSeek)"
                }
                aria-label={`Label ${i + 1}`}
                className="rounded-lg p-2 text-sm outline-none flex-1"
                style={inputStyle}
              />
              <select
                value={m.source_kind}
                onChange={(e) =>
                  setModelWithAutofill(i, {
                    source_kind: e.target.value as "spark" | "agent",
                    spark_model: "",
                    agent_id: null,
                  })
                }
                className="rounded-lg p-2 text-sm outline-none"
                style={inputStyle}
                aria-label={`Quelle ${i + 1}`}
              >
                <option value="spark">Direkt-API (vanilla)</option>
                <option value="agent">Agent</option>
              </select>
              {m.source_kind === "spark" ? (
                sparkOffline ? (
                  // Spark unreachable (or the status probe itself failed,
                  // NIT 1) — free-text fallback plus an inline warning; a
                  // blank model here would 422 at create (backend can't
                  // resolve "auto" either), so `valid` blocks submit until
                  // the operator types a model manually.
                  <div className="flex flex-col gap-1 flex-1">
                    <input
                      value={m.spark_model ?? ""}
                      onChange={(e) => setModelWithAutofill(i, { spark_model: e.target.value || null })}
                      placeholder="vLLM-Modell (leer = aktiv)"
                      aria-label={`vLLM-Modell ${i + 1}`}
                      className="rounded-lg p-2 text-sm outline-none"
                      style={inputStyle}
                    />
                    <span className="text-[11px]" style={{ color: C.warning }}>
                      Spark offline — Modell manuell eintragen oder später starten
                    </span>
                  </div>
                ) : (
                  <select
                    value={m.spark_model ?? ""}
                    onChange={(e) => setModelWithAutofill(i, { spark_model: e.target.value || null })}
                    className="rounded-lg p-2 text-sm outline-none flex-1"
                    style={inputStyle}
                    aria-label={`vLLM-Modell ${i + 1}`}
                  >
                    <option value="">Aktives Modell (auto)</option>
                    {(sparkModels?.models ?? []).map((model) => (
                      <option key={model} value={model}>
                        {model}
                      </option>
                    ))}
                  </select>
                )
              ) : (
                <select
                  value={m.agent_id ?? ""}
                  onChange={(e) => setModelWithAutofill(i, { agent_id: e.target.value || null })}
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
              <input
                value={m.display_tag ?? ""}
                onChange={(e) => setModel(i, { display_tag: e.target.value })}
                placeholder={`Tag (${derivedTag(m)})`}
                aria-label={`Tag ${i + 1}`}
                className="rounded-lg p-2 text-sm outline-none flex-1"
                style={inputStyle}
              />
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

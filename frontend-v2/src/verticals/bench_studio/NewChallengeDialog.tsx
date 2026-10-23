"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { Plus, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import { C, STATUS_TEXT } from "@/lib/colors";
import { notify } from "@/lib/notify";
import { NativeSelect } from "@/components/shared/NativeSelect";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { benchApi } from "@/verticals/bench_studio/api";
import { apiErrorDetail, apiFieldErrors, apiFieldErrorsText, type ApiFieldError } from "./apiError";
import { EffortChips } from "./EffortChips";
import { displayLabel } from "./format";
import type { BenchModelSpec, EffortLevel, EffortLevels, PromptTemplate } from "./types";

const EMPTY_MODEL: BenchModelSpec = { label: "", source_kind: "spark", spark_model: "", display_tag: "" };

// The backend's limits (routers.BenchChallengeCreate / BenchModelSpec) — the
// fields stop there instead of a 422 after "Start" (round-4 t2 SOLL-1).
const TITLE_MAX = 200;
const TITLE_COUNT_FROM = 180;
const LABEL_MAX = 80;
const TAG_MAX = 80;
const SERIES_MAX = 80;

/** A label cut to the backend's limit — create does the same for an auto row. */
const capLabel = (value: string) => value.trim().slice(0, LABEL_MAX).trimEnd();

/** Which dialog field a 422 item belongs to (its `loc` without "body"), or
 *  null when the dialog has no field for it (then the toast says it). */
function fieldKeyOf(path: (string | number)[]): string | null {
  const [head, index, leaf] = path;
  if (path.length === 1) {
    return (
      { title: "title", prompt_text: "prompt", series_label: "series", record_duration_s: "duration" } as Record<
        string,
        string
      >
    )[String(head)] ?? null;
  }
  if (head === "models" && typeof index === "number" && typeof leaf === "string") {
    if (leaf === "label") return `label-${index}`;
    if (leaf === "display_tag") return `tag-${index}`;
    if (leaf === "spark_model" || leaf === "agent_id") return `model-${index}`;
    if (leaf === "effort") return `effort-${index}`;
  }
  return null;
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
  const t = useTranslations("bench.newChallengeDialog");
  const tCommon = useTranslations("bench.common");
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
  // A 422 said at its field (round-4 t2 SOLL-2): field key -> message.
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [focusField, setFocusField] = useState<string | null>(null);

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
          // Effort is a direct-API setting; agent rows record theirs (D5).
          effort: m.source_kind === "spark" ? m.effort ?? null : null,
        })),
        series_label: seriesLabel.trim() || null,
        record_duration_s: recordDurationS,
      }),
    onSuccess: () => {
      notify.success(t("notifyStarted"));
      qc.invalidateQueries({ queryKey: ["bench-challenges"] });
      reset();
      onClose();
    },
    onError: (err) => {
      const items = apiFieldErrors(err);
      const mapped: Record<string, string> = {};
      const unmapped: ApiFieldError[] = [];
      for (const item of items) {
        const key = fieldKeyOf(item.path);
        if (key) mapped[key] ??= fieldMessage(item, key);
        else unmapped.push(item);
      }
      setFieldErrors(mapped);
      const first = Object.keys(mapped)[0];
      if (first) setFocusField(first);
      // The toast is the fallback for what has no field (or is no list). It
      // names only the problems no field shows already (review NICE-1).
      if (items.length === 0) notify.error(apiErrorDetail(err, t("notifyStartFailed")));
      else if (unmapped.length > 0) notify.error(apiFieldErrorsText(unmapped) ?? t("notifyStartFailed"));
    },
  });

  // Move to the first field a 422 named — on a phone it can sit far above.
  useEffect(() => {
    if (!focusField) return;
    document.getElementById(fieldId(focusField))?.focus();
    setFocusField(null);
  }, [focusField]);

  function fieldMessage(item: ApiFieldError, key: string): string {
    // The server's own refusals (routers._row_422) carry a stable type; its
    // English `msg` is never shown (round-5 r1 N2).
    const otherRow = item.ctx?.other_row;
    if (item.type === "bench_duplicate_entry" && typeof otherRow === "number")
      return t("labelDuplicate", { index: otherRow });
    if (item.type === "bench_agent_label_required") return t("fieldError.agentLabelRequired");
    if (item.type === "bench_effort_agent_path") return t("fieldError.effortAgentPath");
    if (item.type === "bench_spark_unreachable") return t("fieldError.sparkUnreachable");
    // Round-7 re-check r1 N3/N4: a failed check is no "not offered", and the
    // last two bare English 400s now come as field refusals too.
    if (item.type === "bench_effort_probe_failed") return t("fieldError.effortProbeFailed");
    if (item.type === "bench_prompt_required") return t("fieldError.promptRequired");
    if (item.type === "bench_agent_required") return t("fieldError.agentRequired");
    // bench_effort_not_offered, and Pydantic's own refusal of an unknown level.
    if (key.startsWith("effort-")) return t("fieldError.effortNotOffered");
    const max = item.ctx?.max_length;
    if (item.type === "string_too_long" && typeof max === "number") return t("fieldError.tooLong", { max });
    if (item.type === "string_too_short" || item.type === "missing") return t("fieldError.required");
    if (/^(greater|less)_than(_equal)?$/.test(item.type)) return t("fieldError.range");
    return t("fieldError.invalid");
  }

  const fieldId = (key: string) => `bench-field-${key}`;
  /** id + aria-invalid + aria-describedby of a field; the message sits under it. */
  function fieldA11y(key: string) {
    const invalid = key in fieldErrors;
    return {
      id: fieldId(key),
      "aria-invalid": invalid || undefined,
      "aria-describedby": invalid ? `${fieldId(key)}-error` : undefined,
    };
  }
  function fieldError(key: string) {
    if (!(key in fieldErrors)) return null;
    return (
      <span id={`${fieldId(key)}-error`} className="text-xs" style={{ color: STATUS_TEXT.error }}>
        {fieldErrors[key]}
      </span>
    );
  }
  function clearFieldErrors(...keys: string[]) {
    setFieldErrors((prev) => {
      if (!keys.some((k) => k in prev)) return prev;
      const next = { ...prev };
      for (const k of keys) delete next[k];
      return next;
    });
  }
  // Rows shift when one is added or removed — their messages would point at
  // the wrong row, so they go (the next Start says them again).
  function clearRowErrors() {
    setFieldErrors((prev) => {
      const kept = Object.entries(prev).filter(([k]) => !/-\d+$/.test(k));
      return kept.length === Object.keys(prev).length ? prev : Object.fromEntries(kept);
    });
  }
  const borderFor = (key: string) =>
    key in fieldErrors ? { ...inputStyle, border: `1px solid ${STATUS_TEXT.error}` } : inputStyle;

  function reset() {
    setTitle("");
    setPromptText("");
    setTemplateId(null);
    setMode("side_by_side");
    setSeriesLabel("");
    setRecordDurationRaw(String(RECORD_DURATION_DEFAULT));
    setModels([{ ...EMPTY_MODEL }]);
    setLabelTouched(new Set());
    setFieldErrors({});
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

  function clearRowFieldErrors(i: number, patch: Partial<BenchModelSpec>) {
    const keys: string[] = [];
    if ("label" in patch) keys.push(`label-${i}`);
    if ("display_tag" in patch) keys.push(`tag-${i}`);
    if ("spark_model" in patch || "agent_id" in patch || "source_kind" in patch) keys.push(`model-${i}`, `label-${i}`);
    // Another level or model answers an effort refusal; a duplicate refusal
    // (said at the label) is about label AND effort.
    if ("effort" in patch || "spark_model" in patch || "source_kind" in patch) keys.push(`effort-${i}`, `label-${i}`);
    if (keys.length) clearFieldErrors(...keys);
  }

  function setModel(i: number, patch: Partial<BenchModelSpec>) {
    clearRowFieldErrors(i, patch);
    setModels((prev) => prev.map((m, idx) => (idx === i ? { ...m, ...patch } : m)));
  }

  // Autofill: übernimmt den gewählten Modell-/Agentennamen ins Label,
  // solange Mark das Label der Zeile nicht selbst angefasst hat.
  function setModelWithAutofill(i: number, patch: Partial<BenchModelSpec>) {
    clearRowFieldErrors(i, patch);
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
            next.label = capLabel(next.spark_model ?? "");
          } else if (next.agent_id) {
            const agent = (agents ?? []).find((a) => a.id === next.agent_id);
            next.label = capLabel(agent?.model ?? agent?.name ?? "");
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

  // The chip tag an empty field gets — mirrors the backend fallback
  // (bench_studio/orchestrator._build_branding_payload): spark -> the local
  // direct-API path (the video writes it "LOCAL · DIRECT API"; the dialog
  // says it in the UI language, r2 N7), agent -> harness uppercased, agent
  // name as last resort.
  function tagPlaceholder(m: BenchModelSpec): string {
    if (m.source_kind === "spark") return t("tagPlaceholderSpark");
    const agent = (agents ?? []).find((a) => a.id === m.agent_id);
    const tag = agent?.harness ?? agent?.name;
    return tag ? t("tagPlaceholder", { tag: tag.toUpperCase() }) : t("tagPlaceholderAgent");
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

  // Every entry has to be told apart (round-1 user test: two "DeepSeek" rows,
  // Off vs Max, overwrote each other). The label a row will really get:
  // typed, or for an untouched auto row the model the backend resolves.
  function effectiveLabel(m: BenchModelSpec): string {
    const typed = m.label.trim();
    if (typed) return typed;
    return isAutoSparkRow(m) ? capLabel(sparkModels?.active ?? "") : "";
  }
  const rowEffort = (m: BenchModelSpec): EffortLevel => (m.source_kind === "spark" ? m.effort ?? "default" : "default");

  // The model a direct-API row really runs: the chosen one, or the box's
  // active model for an auto row (both are the same run).
  const sparkRunModel = (m: BenchModelSpec) => (m.spark_model ?? "").trim() || (sparkModels?.active ?? "").trim();

  /** The row "Add model" appends (round-4 t2 SOLL-3: it used to be a fresh
   *  auto + Default row, a twin of row 1, so the red duplicate hint came up
   *  at once and Start was off). A direct-API row keeps the last row's model
   *  with the next effort level the probe offers that no row of that model
   *  uses yet (after the last row's level, round the ladder). Nothing free:
   *  Default, and the duplicate hint says why. An agent row is followed by
   *  an agent row without an agent — an agent has one level only. */
  function nextModelRow(prev: BenchModelSpec[]): BenchModelSpec {
    const last = prev.at(-1);
    if (!last) return { ...EMPTY_MODEL };
    if (last.source_kind === "agent") return { ...EMPTY_MODEL, source_kind: "agent", spark_model: "", agent_id: null };
    const sparkModel = (last.spark_model ?? "").trim();
    const probe = qc.getQueryData<EffortLevels>(["bench-effort-levels", sparkModel || "active"]);
    const ladder: EffortLevel[] = ["default", ...(probe?.status === "ok" ? probe.levels : [])];
    const used = new Set(
      prev.filter((r) => r.source_kind === "spark" && sparkRunModel(r) === sparkRunModel(last)).map(rowEffort),
    );
    const start = ladder.indexOf(rowEffort(last));
    let effort: EffortLevel = "default";
    for (let step = 1; step <= ladder.length; step++) {
      const level = ladder[(start + step) % ladder.length];
      if (!used.has(level)) {
        effort = level;
        break;
      }
    }
    return {
      ...EMPTY_MODEL,
      spark_model: sparkModel,
      // An explicit model fills the label like a pick would (autofill); an
      // auto row stays empty and shows the active model as placeholder.
      label: capLabel(sparkModel),
      effort: effort === "default" ? null : effort,
    };
  }

  function addModelRow() {
    clearRowErrors();
    setLabelTouched((prev) => {
      // A removed row's "edited by hand" mark must not stick to the new row.
      if (!prev.has(models.length)) return prev;
      const next = new Set(prev);
      next.delete(models.length);
      return next;
    });
    setModels((prev) => [...prev, nextModelRow(prev)]);
  }

  function removeModelRow(i: number) {
    clearRowErrors();
    setLabelTouched((prev) => new Set([...prev].filter((idx) => idx !== i).map((idx) => (idx > i ? idx - 1 : idx))));
    setModels((prev) => prev.filter((_, idx) => idx !== i));
  }
  // Since round-2 Q2 the effort is its own field: the same model twice with
  // different efforts is two entries (the UI and the video show the effort
  // next to the name) and needs no hint. Only the same label AND the same
  // effort as an earlier row is a duplicate — the backend refuses it
  // (routers._unique_labels): the box answers deterministically, the second
  // row would repeat the first and nobody could tell them apart.
  // The label is compared as the backend shows it (display_label: an old
  // "DeepSeek · Max" on a Max row is "DeepSeek", t2 KANN-1).
  const labelKey = (m: BenchModelSpec) => displayLabel(effectiveLabel(m), rowEffort(m)).trim().toLowerCase();
  const duplicateOf = models.map((m, i): number | null => {
    const key = labelKey(m);
    if (!key) return null;
    const twin = models.findIndex((o, j) => j < i && labelKey(o) === key && rowEffort(o) === rowEffort(m));
    return twin >= 0 ? twin + 1 : null;
  });
  const hasDuplicateLabel = duplicateOf.some((index) => index !== null);

  const valid =
    !hasDuplicateLabel &&
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

  // Every field is at least 44 px tall (K11 finger targets).
  const fieldClass = "rounded-lg px-3 min-h-11 text-sm outline-none w-full min-w-0";

  return (
    <ResponsiveModal dismissOnOutside={false} open={open} onClose={onClose} aria-label={t("ariaLabel")}>
      {/* Plain scroll box with an uncapped column inside. The fields used to
          be children of the height-capped flex column itself, so on a phone
          they shrank (template 18 px, prompt 26 px — round-1 t2 MUSS-4 /
          r2 M1). */}
      <div
        className="rounded-xl w-full max-h-[85vh] overflow-y-auto"
        style={{ backgroundColor: C.bgElevated, border: `1px solid ${C.border}` }}
      >
      <div className="flex flex-col gap-4 p-4 sm:p-6">
        <h3 className="text-base font-semibold" style={{ color: C.textPrimary }}>
          {t("title")}
        </h3>

        <div className="flex flex-col gap-1">
          <input
            {...fieldA11y("title")}
            value={title}
            maxLength={TITLE_MAX}
            onChange={(e) => {
              setTitle(e.target.value);
              clearFieldErrors("title");
            }}
            placeholder={t("titlePlaceholder")}
            className={fieldClass}
            style={borderFor("title")}
          />
          {(fieldErrors.title || title.length >= TITLE_COUNT_FROM) && (
            <div className="flex items-start justify-between gap-2">
              {fieldError("title") ?? <span />}
              {title.length >= TITLE_COUNT_FROM && (
                <span className="text-xs tabular-nums shrink-0" style={{ color: C.textMuted }}>
                  {title.length}/{TITLE_MAX}
                </span>
              )}
            </div>
          )}
        </div>

        {/* Template picker: "Freitext" default + one option per template */}
        <NativeSelect
          value={templateId ?? ""}
          onChange={(e) => handleTemplateSelect(e.target.value)}
          className={fieldClass}
          style={inputStyle}
          aria-label={t("templateSelectAria")}
        >
          <option value="">{t("freetextOption")}</option>
          {(templates ?? []).map((tpl) => (
            <option key={tpl.id} value={tpl.id}>
              {tpl.title}
            </option>
          ))}
        </NativeSelect>

        <textarea
          {...fieldA11y("prompt")}
          value={promptText}
          onChange={(e) => {
            setPromptText(e.target.value);
            clearFieldErrors("prompt");
            // Don't clear templateId — edited text + template ID both win (backend uses text if provided)
          }}
          rows={5}
          placeholder={t("promptPlaceholder")}
          className="rounded-lg p-3 text-sm resize-none outline-none w-full"
          style={borderFor("prompt")}
        />
        {fieldError("prompt")}
        {templateId && (
          <span className="text-xs" style={{ color: C.textMuted }}>
            {t("fromTemplateHint")}
          </span>
        )}

        {/* Phone: one field per line (the video length sat at x=383 on a
            390 px screen). From sm up: one row. */}
        <div data-testid="bench-options-row" className="flex flex-col sm:flex-row gap-3 sm:items-end">
          <NativeSelect
            value={mode}
            onChange={(e) => setMode(e.target.value as "single" | "side_by_side")}
            wrapperClassName="sm:flex-1"
            className={fieldClass}
            style={inputStyle}
            aria-label={t("modeAria")}
          >
            <option value="side_by_side">{t("modeSideBySide")}</option>
            <option value="single">{t("modeSingle")}</option>
          </NativeSelect>
          <div className="flex flex-col gap-1 sm:flex-1 min-w-0">
            <input
              {...fieldA11y("series")}
              value={seriesLabel}
              maxLength={SERIES_MAX}
              onChange={(e) => {
                setSeriesLabel(e.target.value);
                clearFieldErrors("series");
              }}
              placeholder={t("seriesLabelPlaceholder")}
              className={fieldClass}
              style={borderFor("series")}
            />
            {fieldError("series")}
          </div>
          <div className="flex flex-col gap-1 sm:w-32">
            <label htmlFor="bench-record-duration" className="text-xs" style={{ color: C.textMuted }}>
              {t("videoLengthLabel")}
            </label>
            <input
              id="bench-record-duration"
              aria-invalid={"duration" in fieldErrors || undefined}
              aria-describedby={"duration" in fieldErrors ? `${fieldId("duration")}-error` : undefined}
              type="number"
              min={RECORD_DURATION_MIN}
              max={RECORD_DURATION_MAX}
              step={1}
              value={recordDurationRaw}
              onChange={(e) => {
                setRecordDurationRaw(e.target.value);
                clearFieldErrors("duration");
              }}
              onBlur={() => setRecordDurationRaw(String(clampRecordDuration(recordDurationRaw)))}
              className={fieldClass}
              style={borderFor("duration")}
            />
            {fieldError("duration")}
          </div>
        </div>

        <div className="flex flex-col">
          <span className="text-sm font-medium" style={{ color: C.textSecondary }}>
            {t("modelsLabel")}
          </span>
          {models.map((m, i) => (
            // One block per model with its own heading; a hairline between
            // models so model 1's effort chips do not run into model 2's
            // fields (t2 SOLL-5).
            <div
              key={i}
              data-testid="bench-model-row"
              className={`flex flex-col gap-2 py-3${i > 0 ? " border-t" : ""}`}
              style={i > 0 ? { borderColor: C.borderSubtle } : undefined}
            >
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs font-medium" style={{ color: C.textMuted }}>
                {t("modelHeading", { index: i + 1 })}
              </span>
              <button
                onClick={() => removeModelRow(i)}
                disabled={models.length <= 1}
                aria-label={t("removeModelAria", { index: i + 1 })}
                className="flex items-center justify-center min-h-11 min-w-11 -mr-3 rounded-md disabled:opacity-30"
                style={{ color: C.textMuted }}
              >
                <Trash2 size={16} />
              </button>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              <div className="flex flex-col gap-1 min-w-0">
              <input
                {...fieldA11y(`label-${i}`)}
                value={m.label}
                maxLength={LABEL_MAX}
                onChange={(e) => {
                  markLabelTouched(i, e.target.value);
                  setModel(i, { label: e.target.value });
                }}
                placeholder={
                  // Untouched auto row: preview the model the backend will
                  // resolve at create time — as a placeholder only, never
                  // written into the submitted value (MINOR 1).
                  isAutoSparkRow(m) && !sparkOffline && sparkModels?.active
                    ? t("labelPlaceholderAuto", { model: sparkModels.active })
                    : t("labelPlaceholderDefault")
                }
                aria-label={t("labelAria", { index: i + 1 })}
                className={fieldClass}
                style={borderFor(`label-${i}`)}
              />
              {fieldError(`label-${i}`)}
              </div>
              <NativeSelect
                value={m.source_kind}
                onChange={(e) =>
                  setModelWithAutofill(i, {
                    source_kind: e.target.value as "spark" | "agent",
                    spark_model: "",
                    agent_id: null,
                    effort: null,
                  })
                }
                className={fieldClass}
                style={inputStyle}
                aria-label={t("sourceAria", { index: i + 1 })}
              >
                <option value="spark">{t("sourceSpark")}</option>
                <option value="agent">{t("sourceAgent")}</option>
              </NativeSelect>
              {m.source_kind === "spark" ? (
                sparkOffline ? (
                  // Spark unreachable (or the status probe itself failed,
                  // NIT 1) — free-text fallback plus an inline warning; a
                  // blank model here would 422 at create (backend can't
                  // resolve "auto" either), so `valid` blocks submit until
                  // the operator types a model manually.
                  <div className="flex flex-col gap-1 min-w-0">
                    <input
                      {...fieldA11y(`model-${i}`)}
                      value={m.spark_model ?? ""}
                      onChange={(e) => setModelWithAutofill(i, { spark_model: e.target.value || null })}
                      placeholder={t("vllmModelPlaceholderOffline")}
                      aria-label={t("vllmModelAria", { index: i + 1 })}
                      className={fieldClass}
                      style={borderFor(`model-${i}`)}
                    />
                    {fieldError(`model-${i}`)}
                    <span className="text-xs" style={{ color: STATUS_TEXT.warning }}>
                      {t("sparkOfflineWarning")}
                    </span>
                  </div>
                ) : (
                  // Every field that can turn red also says why (review SHOULD-1).
                  <div className="flex flex-col gap-1 min-w-0">
                    <NativeSelect
                      {...fieldA11y(`model-${i}`)}
                      value={m.spark_model ?? ""}
                      onChange={(e) => setModelWithAutofill(i, { spark_model: e.target.value || null })}
                      className={fieldClass}
                      style={borderFor(`model-${i}`)}
                      aria-label={t("vllmModelAria", { index: i + 1 })}
                    >
                      <option value="">{t("activeModelAuto")}</option>
                      {(sparkModels?.models ?? []).map((model) => (
                        <option key={model} value={model}>
                          {model}
                        </option>
                      ))}
                    </NativeSelect>
                    {fieldError(`model-${i}`)}
                  </div>
                )
              ) : (
                <div className="flex flex-col gap-1 min-w-0">
                  <NativeSelect
                    {...fieldA11y(`model-${i}`)}
                    value={m.agent_id ?? ""}
                    onChange={(e) => setModelWithAutofill(i, { agent_id: e.target.value || null })}
                    className={fieldClass}
                    style={borderFor(`model-${i}`)}
                    aria-label={t("agentAria", { index: i + 1 })}
                  >
                    <option value="">{t("agentSelectPlaceholder")}</option>
                    {(agents ?? []).map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.name}
                      </option>
                    ))}
                  </NativeSelect>
                  {fieldError(`model-${i}`)}
                </div>
              )}
              <div className="flex flex-col gap-1 min-w-0">
                <input
                  {...fieldA11y(`tag-${i}`)}
                  value={m.display_tag ?? ""}
                  maxLength={TAG_MAX}
                  onChange={(e) => setModel(i, { display_tag: e.target.value })}
                  placeholder={tagPlaceholder(m)}
                  aria-label={t("tagAria", { index: i + 1 })}
                  className={fieldClass}
                  style={borderFor(`tag-${i}`)}
                />
                {fieldError(`tag-${i}`)}
              </div>
            </div>
            <EffortChips
              index={i}
              sourceKind={m.source_kind}
              model={m.spark_model}
              sparkOffline={sparkOffline}
              value={m.effort}
              onChange={(effort) => setModel(i, { effort })}
              {...fieldA11y(`effort-${i}`)}
            />
            {fieldError(`effort-${i}`)}
            {/* The duplicate hint closes the model's own block (above the
                divider to the next model); error text tone via STATUS_TEXT. */}
            {duplicateOf[i] !== null && (
              <span className="text-xs" role="alert" style={{ color: STATUS_TEXT.error }}>
                {t("labelDuplicate", { index: duplicateOf[i] ?? 0 })}
              </span>
            )}
            </div>
          ))}
          <button
            onClick={addModelRow}
            disabled={models.length >= 6}
            className="self-start flex items-center gap-2 min-h-11 text-sm disabled:opacity-40"
            style={{ color: C.accent }}
          >
            <Plus size={14} /> {t("addModel")}
          </button>
        </div>

        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="px-4 min-h-11 rounded-lg text-sm"
            style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
          >
            {tCommon("cancel")}
          </button>
          <button
            onClick={() => mutation.mutate()}
            disabled={!valid || mutation.isPending}
            className="px-4 min-h-11 rounded-lg text-sm font-medium disabled:opacity-40"
            style={{ backgroundColor: C.accentSubtle, color: C.accent, border: `1px solid ${C.borderAccent}` }}
          >
            {t("startChallenge")}
          </button>
        </div>
      </div>
      </div>
    </ResponsiveModal>
  );
}

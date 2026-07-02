"use client";

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Plus,
  Trash2,
  RefreshCw,
  AlertCircle,
  Check,
  Loader2,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import type { ModelPrice, ModelPriceCreate, UnmatchedModel } from "@/lib/types";
import { C } from "@/lib/colors";
import { cn } from "@/lib/utils";

// Shared styles (1:1 aus settings/page.tsx)
const cardStyle = {
  background: C.bgSurface,
  border: `1px solid ${C.border}`,
  borderRadius: 12,
} as const;

const inputBaseClasses =
  "w-full rounded-lg px-3 py-2.5 text-sm outline-none transition-all duration-200";

// ── Input-Hilfskomponenten ────────────────────────────────────────────────

function InputNumber({
  value,
  onChange,
  ariaLabel,
  step = "0.01",
}: {
  value: number;
  onChange: (v: number) => void;
  ariaLabel: string;
  step?: string;
}) {
  return (
    <input
      type="number"
      step={step}
      value={value}
      onChange={(e) => onChange(parseFloat(e.target.value) || 0)}
      aria-label={ariaLabel}
      className={inputBaseClasses}
      style={{
        backgroundColor: C.bgDeep,
        borderWidth: 1,
        borderStyle: "solid",
        borderColor: "rgba(255, 255, 255, 0.08)",
        color: "var(--color-text-primary)",
        minHeight: 44,
      }}
      onFocus={(e) => {
        e.currentTarget.style.borderColor = C.borderAccent;
      }}
      onBlur={(e) => {
        e.currentTarget.style.borderColor = "rgba(255, 255, 255, 0.08)";
      }}
    />
  );
}

function TextInput({
  value,
  onChange,
  placeholder,
  ariaLabel,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  ariaLabel: string;
}) {
  return (
    <input
      type="text"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      aria-label={ariaLabel}
      className={inputBaseClasses}
      style={{
        backgroundColor: C.bgDeep,
        borderWidth: 1,
        borderStyle: "solid",
        borderColor: "rgba(255, 255, 255, 0.08)",
        color: "var(--color-text-primary)",
        minHeight: 44,
      }}
      onFocus={(e) => {
        e.currentTarget.style.borderColor = C.borderAccent;
      }}
      onBlur={(e) => {
        e.currentTarget.style.borderColor = "rgba(255, 255, 255, 0.08)";
      }}
    />
  );
}

// ── Preis-Zeile (readonly) ────────────────────────────────────────────────

function PriceRow({
  price,
  onEdit,
  onDelete,
}: {
  price: ModelPrice;
  onEdit: () => void;
  onDelete: () => void;
}) {
  return (
    <tr
      style={{ borderBottom: `1px solid ${C.borderSubtle}` }}
      className="transition-colors"
      onMouseEnter={(e) => (e.currentTarget.style.background = "rgba(255,255,255,0.02)")}
      onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
    >
      <td
        className="px-3 py-2.5 text-sm font-mono"
        style={{ color: C.accent, whiteSpace: "nowrap" }}
      >
        {price.model_pattern}
      </td>
      <td
        className="px-3 py-2.5 text-sm tabular-nums"
        style={{ color: "var(--color-text-body)" }}
      >
        ${price.input_per_mtok}
      </td>
      <td
        className="px-3 py-2.5 text-sm tabular-nums"
        style={{ color: "var(--color-text-body)" }}
      >
        ${price.output_per_mtok}
      </td>
      <td
        className="px-3 py-2.5 text-sm tabular-nums"
        style={{ color: "var(--color-text-body)" }}
      >
        ${price.cache_read_per_mtok}
      </td>
      <td
        className="px-3 py-2.5 text-sm tabular-nums"
        style={{ color: "var(--color-text-body)" }}
      >
        ${price.cache_write_per_mtok}
      </td>
      <td
        className="px-3 py-2.5 text-sm tabular-nums text-center"
        style={{ color: "var(--color-text-muted)" }}
      >
        {price.priority}
      </td>
      <td className="px-3 py-2.5 text-xs" style={{ color: "var(--color-text-muted)" }}>
        {price.valid_from.slice(0, 10)}
      </td>
      <td
        className="px-3 py-2.5 text-xs max-w-[120px] truncate"
        style={{ color: "var(--color-text-muted)" }}
      >
        {price.note ?? "—"}
      </td>
      <td className="px-3 py-2.5">
        <div className="flex items-center gap-1">
          <button
            onClick={onEdit}
            aria-label={`Preis fuer ${price.model_pattern} bearbeiten`}
            className="px-2 py-1 rounded text-xs cursor-pointer transition-colors"
            style={{
              color: "var(--color-text-secondary)",
              minHeight: 32,
              minWidth: 32,
            }}
          >
            Bearb.
          </button>
          <button
            onClick={onDelete}
            aria-label={`Preis fuer ${price.model_pattern} loeschen`}
            className="px-2 py-1 rounded text-xs cursor-pointer transition-colors"
            style={{ color: C.error, minHeight: 32, minWidth: 32 }}
          >
            <Trash2 size={12} />
          </button>
        </div>
      </td>
    </tr>
  );
}

// ── Add/Edit-Formular ─────────────────────────────────────────────────────

const EMPTY_FORM: ModelPriceCreate = {
  model_pattern: "",
  input_per_mtok: 0,
  output_per_mtok: 0,
  cache_read_per_mtok: 0,
  cache_write_per_mtok: 0,
  valid_from: new Date().toISOString().slice(0, 10) + "T00:00:00Z",
  priority: 50,
  note: "",
};

function AddPriceForm({
  initial,
  onSave,
  onCancel,
  isLoading,
}: {
  initial?: Partial<ModelPriceCreate>;
  onSave: (data: ModelPriceCreate) => void;
  onCancel: () => void;
  isLoading: boolean;
}) {
  const [form, setForm] = useState<ModelPriceCreate>({ ...EMPTY_FORM, ...initial });

  const update = (patch: Partial<ModelPriceCreate>) =>
    setForm((prev) => ({ ...prev, ...patch }));

  return (
    <div
      className="p-4 rounded-xl space-y-3"
      style={{ background: C.bgElevated, border: `1px solid ${C.borderAccent}` }}
    >
      <div
        className="text-xs font-semibold uppercase tracking-wider mb-2"
        style={{ color: C.accent }}
      >
        {initial?.model_pattern ? "Preis bearbeiten" : "Neuer Preis"}
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label
            className="text-xs font-medium block mb-1"
            style={{ color: "var(--color-text-secondary)" }}
          >
            Modell-Pattern
          </label>
          <TextInput
            value={form.model_pattern}
            onChange={(v) => update({ model_pattern: v })}
            placeholder="claude-sonnet-4-* oder exakt"
            ariaLabel="Modell-Pattern"
          />
        </div>
        <div>
          <label
            className="text-xs font-medium block mb-1"
            style={{ color: "var(--color-text-secondary)" }}
          >
            Notiz (optional)
          </label>
          <TextInput
            value={form.note ?? ""}
            onChange={(v) => update({ note: v || null })}
            placeholder="z.B. lokal / Flatrate"
            ariaLabel="Notiz"
          />
        </div>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {(
          [
            ["input_per_mtok", "Input $/Mtok"],
            ["output_per_mtok", "Output $/Mtok"],
            ["cache_read_per_mtok", "Cache-R $/Mtok"],
            ["cache_write_per_mtok", "Cache-W $/Mtok"],
          ] as const
        ).map(([field, label]) => (
          <div key={field}>
            <label
              className="text-xs font-medium block mb-1"
              style={{ color: "var(--color-text-secondary)" }}
            >
              {label}
            </label>
            <InputNumber
              value={form[field]}
              onChange={(v) => update({ [field]: v })}
              ariaLabel={label}
              step="0.001"
            />
          </div>
        ))}
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label
            className="text-xs font-medium block mb-1"
            style={{ color: "var(--color-text-secondary)" }}
          >
            Prioritaet
          </label>
          <InputNumber
            value={form.priority}
            onChange={(v) => update({ priority: Math.round(v) })}
            ariaLabel="Prioritaet (hoeher = spezifischer)"
            step="1"
          />
        </div>
        <div>
          <label
            className="text-xs font-medium block mb-1"
            style={{ color: "var(--color-text-secondary)" }}
          >
            Gueltig ab
          </label>
          <input
            type="date"
            value={form.valid_from.slice(0, 10)}
            onChange={(e) => update({ valid_from: e.target.value + "T00:00:00Z" })}
            aria-label="Gueltig ab (Datum)"
            className={inputBaseClasses}
            style={{
              backgroundColor: C.bgDeep,
              borderWidth: 1,
              borderStyle: "solid",
              borderColor: "rgba(255, 255, 255, 0.08)",
              color: "var(--color-text-primary)",
              minHeight: 44,
            }}
          />
        </div>
      </div>
      <div className="flex items-center gap-2 pt-1">
        <button
          onClick={() => onSave(form)}
          disabled={!form.model_pattern || isLoading}
          aria-label="Preis speichern"
          className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium text-white cursor-pointer transition-all duration-200 disabled:opacity-40 disabled:cursor-not-allowed"
          style={{
            background: `linear-gradient(135deg, ${C.accent}, ${C.accentHover})`,
            minHeight: 44,
          }}
        >
          {isLoading ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}
          Speichern
        </button>
        <button
          onClick={onCancel}
          aria-label="Abbrechen"
          className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm cursor-pointer"
          style={{
            color: "var(--color-text-muted)",
            border: `1px solid ${C.border}`,
            minHeight: 44,
          }}
        >
          <X size={14} /> Abbrechen
        </button>
      </div>
    </div>
  );
}

// ── Hauptkomponente ───────────────────────────────────────────────────────

export function CostPricesTab() {
  const qc = useQueryClient();
  const [showAddForm, setShowAddForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [recomputeResult, setRecomputeResult] = useState<number | null>(null);

  const { data: prices, isLoading: loadingPrices } = useQuery({
    queryKey: ["model-prices"],
    queryFn: api.modelPrices.list,
  });

  const { data: unmatched } = useQuery({
    queryKey: ["model-prices-unmatched"],
    queryFn: api.modelPrices.unmatched,
  });

  const createMutation = useMutation({
    mutationFn: api.modelPrices.create,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["model-prices"] });
      qc.invalidateQueries({ queryKey: ["model-prices-unmatched"] });
      setShowAddForm(false);
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: ModelPriceCreate }) =>
      api.modelPrices.update(id, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["model-prices"] });
      setEditingId(null);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: api.modelPrices.delete,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["model-prices"] });
      qc.invalidateQueries({ queryKey: ["model-prices-unmatched"] });
    },
  });

  const recomputeMutation = useMutation({
    mutationFn: () => api.modelPrices.recompute(),
    onSuccess: (data) => {
      setRecomputeResult(data.updated);
      qc.invalidateQueries({ queryKey: ["intelligence-costs"] });
      setTimeout(() => setRecomputeResult(null), 5000);
    },
  });

  const handleDelete = (price: ModelPrice) => {
    if (window.confirm(`Preis fuer "${price.model_pattern}" wirklich loeschen?`)) {
      deleteMutation.mutate(price.id);
    }
  };

  const handleAddFromUnmatched = (model: UnmatchedModel) => {
    setShowAddForm(true);
    setTimeout(() => {
      document.getElementById("cost-add-form")?.scrollIntoView({ behavior: "smooth" });
    }, 100);
  };

  return (
    <div className="space-y-6">
      {/* ── Preistabelle ── */}
      <div className="mc-card" style={cardStyle}>
        <div
          className="px-5 py-4 flex items-center justify-between border-b"
          style={{ borderColor: C.borderSubtle }}
        >
          <div>
            <div className="text-sm font-semibold" style={{ color: "var(--color-text-primary)" }}>
              Modell-Preistabelle
            </div>
            <div className="text-xs mt-0.5" style={{ color: "var(--color-text-muted)" }}>
              USD / 1M Tokens. Glob-Pattern: spezifischeres Pattern (hoehere Prioritaet) gewinnt.
            </div>
          </div>
          <button
            onClick={() => setShowAddForm(!showAddForm)}
            aria-label={showAddForm ? "Formular schliessen" : "Neuen Preis hinzufuegen"}
            className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-medium cursor-pointer transition-all duration-200"
            style={{
              background: showAddForm
                ? "rgba(255,255,255,0.04)"
                : `linear-gradient(135deg, ${C.accent}, ${C.accentHover})`,
              color: showAddForm ? "var(--color-text-muted)" : "white",
              border: showAddForm ? `1px solid ${C.border}` : "none",
              minHeight: 36,
            }}
          >
            {showAddForm ? <X size={12} /> : <Plus size={12} />}
            {showAddForm ? "Abbrechen" : "Hinzufuegen"}
          </button>
        </div>

        {/* Add-Formular */}
        {showAddForm && (
          <div id="cost-add-form" className="p-4 border-b" style={{ borderColor: C.borderSubtle }}>
            <AddPriceForm
              onSave={(data) => createMutation.mutate(data)}
              onCancel={() => setShowAddForm(false)}
              isLoading={createMutation.isPending}
            />
          </div>
        )}

        {/* Tabelle — horizontal scrollbar auf Mobile */}
        {loadingPrices ? (
          <div className="flex items-center justify-center py-10">
            <Loader2
              className="animate-spin"
              size={20}
              style={{ color: "var(--color-text-muted)" }}
            />
          </div>
        ) : (prices?.length ?? 0) === 0 ? (
          <div
            className="px-5 py-10 text-center text-sm"
            style={{ color: "var(--color-text-muted)" }}
          >
            Noch keine Preise konfiguriert.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full" style={{ minWidth: 720 }}>
              <thead>
                <tr style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
                  {[
                    "Pattern",
                    "Input $/M",
                    "Output $/M",
                    "Cache-R $/M",
                    "Cache-W $/M",
                    "Prio",
                    "Ab",
                    "Notiz",
                    "",
                  ].map((h, i) => (
                    <th
                      key={h + i}
                      className={cn(
                        "px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider",
                        i === 0 ? "sticky left-0" : ""
                      )}
                      style={{
                        color: "var(--color-text-muted)",
                        background: i === 0 ? C.bgSurface : undefined,
                      }}
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {prices!.map((price) =>
                  editingId === price.id ? (
                    <tr key={price.id} style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
                      <td colSpan={9} className="p-3">
                        <AddPriceForm
                          initial={price}
                          onSave={(data) => updateMutation.mutate({ id: price.id, data })}
                          onCancel={() => setEditingId(null)}
                          isLoading={updateMutation.isPending}
                        />
                      </td>
                    </tr>
                  ) : (
                    <PriceRow
                      key={price.id}
                      price={price}
                      onEdit={() => setEditingId(price.id)}
                      onDelete={() => handleDelete(price)}
                    />
                  )
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── Modelle ohne Preis ── */}
      {(unmatched?.length ?? 0) > 0 && (
        <div className="mc-card" style={cardStyle}>
          <div className="px-5 py-4 border-b" style={{ borderColor: C.borderSubtle }}>
            <div className="flex items-center gap-2">
              <AlertCircle size={14} style={{ color: C.warning }} />
              <span
                className="text-sm font-semibold"
                style={{ color: "var(--color-text-primary)" }}
              >
                Erkannte Modelle ohne Preis
              </span>
              <span
                className="text-[10px] px-1.5 py-0.5 rounded tabular-nums"
                style={{ backgroundColor: `${C.warning}1F`, color: C.warning }}
              >
                {unmatched!.length}
              </span>
            </div>
            <p className="text-xs mt-1" style={{ color: "var(--color-text-muted)" }}>
              Diese Modelle haben Events, aber keinen matchenden Preis-Eintrag → Kosten werden als
              NULL gerechnet.
            </p>
          </div>
          <div className="divide-y" style={{ borderColor: C.borderSubtle }}>
            {unmatched!.map((m) => (
              <div
                key={m.model}
                className="px-5 py-3 flex items-center justify-between gap-3"
              >
                <div className="min-w-0">
                  <code
                    className="text-sm font-mono"
                    style={{ color: "var(--color-text-primary)" }}
                  >
                    {m.model}
                  </code>
                  <div className="text-xs mt-0.5" style={{ color: "var(--color-text-muted)" }}>
                    {m.event_count.toLocaleString("de-CH")} Events ·{" "}
                    {m.total_input_tokens.toLocaleString("de-CH")} Input-Tokens
                  </div>
                </div>
                <button
                  onClick={() => handleAddFromUnmatched(m)}
                  aria-label={`Preis fuer ${m.model} anlegen`}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium cursor-pointer shrink-0"
                  style={{
                    backgroundColor: C.accentSubtle,
                    color: C.accent,
                    minHeight: 36,
                  }}
                >
                  <Plus size={12} /> Preis anlegen
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Kosten neu berechnen ── */}
      <div
        className="mc-card p-5 flex items-center justify-between"
        style={cardStyle}
      >
        <div>
          <div className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
            Kosten neu berechnen
          </div>
          <p className="text-xs mt-0.5" style={{ color: "var(--color-text-muted)" }}>
            cost_usd aller Events mit der aktuellen Preistabelle neu rechnen (nach Preisaenderungen).
          </p>
          {recomputeResult !== null && (
            <p className="text-xs mt-1" style={{ color: C.online }}>
              {recomputeResult.toLocaleString("de-CH")} Events aktualisiert.
            </p>
          )}
        </div>
        <button
          onClick={() => {
            if (
              window.confirm(
                "Alle Event-Kosten mit der aktuellen Preistabelle neu berechnen?"
              )
            ) {
              recomputeMutation.mutate();
            }
          }}
          disabled={recomputeMutation.isPending}
          aria-label="Kosten neu berechnen"
          className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
          style={{
            backgroundColor: "transparent",
            color: "var(--color-text-primary)",
            border: `1px solid ${C.border}`,
            minHeight: 44,
          }}
        >
          {recomputeMutation.isPending ? (
            <Loader2 size={14} className="animate-spin" />
          ) : recomputeResult !== null ? (
            <Check size={14} style={{ color: C.online }} />
          ) : (
            <RefreshCw size={14} />
          )}
          {recomputeMutation.isPending
            ? "Berechne..."
            : recomputeResult !== null
            ? "Erledigt"
            : "Jetzt berechnen"}
        </button>
      </div>
    </div>
  );
}

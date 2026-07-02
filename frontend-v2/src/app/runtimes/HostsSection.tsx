"use client";

/**
 * Host Registry UI (ADR-048) — generische Multi-Host Control-Plane.
 *
 * - HostMetricsBar: eine Metrics-Bar pro enabled Host (ersetzt die alte,
 *   hart verdrahtete SparkMetricsBar). 0 Hosts → rendert nichts — ein
 *   Fresh-Install ohne GPU-Box zeigt kein Empty-Gerippe.
 * - HostsSection: Cards (Name, Kind-Badge, Status, gebundene Runtimes) +
 *   Add/Edit-Modal (admin-only) + Delete mit 409-Guard-Feedback.
 */

import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Pencil, Plus, Server, Trash2, WifiOff, X } from "lucide-react";
import { api } from "@/lib/api";
import type { Host, HostCreate, HostKind, HostMetrics } from "@/lib/types";
import { useAppStore } from "@/lib/store";
import { C, STATUS, STATUS_TEXT } from "@/lib/colors";

// ── Helpers ───────────────────────────────────────────────────────────────────

function barColor(pct: number): string {
  if (pct > 85) return C.error;
  if (pct >= 60) return C.warning;
  return C.online;
}

/** Pull the human-readable detail out of `API 409: {"detail":"..."}` errors. */
function extractApiError(err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err);
  const jsonStart = msg.indexOf("{");
  if (jsonStart >= 0) {
    try {
      const parsed = JSON.parse(msg.slice(jsonStart));
      if (typeof parsed.detail === "string") return parsed.detail;
    } catch {
      /* fall through to raw message */
    }
  }
  return msg;
}

const KIND_LABEL: Record<HostKind, string> = {
  ssh: "SSH",
  flask_wol: "Flask/WoL",
  local: "Local",
};

// ── Host Metrics Bar ──────────────────────────────────────────────────────────

function SingleHostMetricsBar({ host }: { host: Host }) {
  const { data } = useQuery<HostMetrics>({
    queryKey: ["host-metrics", host.id],
    queryFn: () => api.hosts.metrics(host.id),
    refetchInterval: 5_000,
  });

  const barStyle = (pct: number) => ({
    width: `${Math.min(pct, 100)}%`,
    background: barColor(pct),
    transition: "width 0.6s cubic-bezier(0.16,1,0.3,1)",
  });

  // flask_wol hosts report awake/health instead of GPU metrics — checked
  // BEFORE the reachable early-return: the backend maps reachable=awake, so a
  // sleeping power-managed box (reachable=false, status="asleep") is the
  // NORMAL idle state (ADR-042), not an outage row.
  if (host.kind === "flask_wol" && data) {
    const awake = data.awake ?? false;
    return (
      <div
        className="flex items-center gap-3 px-4 py-3 rounded-xl"
        style={{ background: C.borderSubtle, border: `1px solid ${C.borderSubtle}` }}
      >
        <div className="w-1.5 h-1.5 rounded-full" style={{ background: awake ? C.online : STATUS.offline }} />
        <span className="text-xs font-medium tracking-wider uppercase" style={{ color: C.textMuted, letterSpacing: "0.08em" }}>
          {host.display_name}
        </span>
        <span className="text-xs ml-auto" style={{ color: awake ? C.online : C.textMuted }}>
          {awake ? "Wach" : "Schläft"}
        </span>
      </div>
    );
  }

  if (!data || !data.reachable) {
    return (
      <div
        className="flex items-center gap-3 px-4 py-3 rounded-xl"
        style={{ background: C.borderSubtle, border: `1px solid ${C.borderSubtle}` }}
      >
        <WifiOff size={13} style={{ color: C.textMuted }} />
        <span className="text-xs" style={{ color: C.textMuted }}>
          {host.display_name} nicht erreichbar
        </span>
      </div>
    );
  }

  const ramPct = data.ram_total_mb && data.ram_used_mb != null
    ? Math.round((data.ram_used_mb / data.ram_total_mb) * 100) : 0;
  const ramUsedGb = data.ram_used_mb != null ? (data.ram_used_mb / 1024).toFixed(0) : "—";
  const ramTotalGb = data.ram_total_mb != null ? (data.ram_total_mb / 1024).toFixed(0) : "—";
  const gpuPct = data.gpu_util_pct ?? 0;

  return (
    <div
      className="flex flex-col md:flex-row md:items-stretch gap-0 rounded-xl overflow-hidden"
      style={{ background: C.borderSubtle, border: `1px solid ${C.borderSubtle}` }}
    >
      {/* Label — mobil eigene Zeile, Desktop links wie bisher */}
      <div
        className="flex items-center gap-2 px-4 py-2.5 md:py-3 shrink-0 border-b md:border-b-0 md:border-r"
        style={{ borderColor: C.borderSubtle }}
      >
        <div className="w-1.5 h-1.5 rounded-full" style={{ background: C.online }} />
        <span className="text-xs font-medium tracking-wider uppercase" style={{ color: C.textMuted, letterSpacing: "0.08em" }}>
          {host.display_name}
        </span>
      </div>

      {/* Stats — mobil 3er-Grid (passt ohne Scroll), Desktop flex wie bisher */}
      <div className="grid grid-cols-3 md:flex md:flex-1">
      {/* GPU */}
      <div className="md:flex-1 md:min-w-[7.5rem] px-3 md:px-5 py-3" style={{ borderRight: `1px solid ${C.borderSubtle}` }}>
        <div className="flex items-baseline justify-between mb-1.5">
          <span className="text-xs" style={{ color: C.textMuted }}>GPU</span>
          <span className="text-sm font-semibold tabular-nums whitespace-nowrap" style={{ color: barColor(gpuPct) }}>
            {data.gpu_util_pct != null ? `${data.gpu_util_pct}%` : "—"}
          </span>
        </div>
        <div className="h-0.5 rounded-full overflow-hidden" style={{ background: C.border }}>
          <div className="h-full rounded-full" style={barStyle(gpuPct)} />
        </div>
      </div>

      {/* RAM */}
      <div className="md:flex-1 md:min-w-[7.5rem] px-3 md:px-5 py-3" style={{ borderRight: `1px solid ${C.borderSubtle}` }}>
        <div className="flex items-baseline justify-between mb-1.5">
          <span className="text-xs" style={{ color: C.textMuted }}>RAM</span>
          <span className="text-sm font-semibold tabular-nums whitespace-nowrap" style={{ color: barColor(ramPct) }}>
            {ramUsedGb}<span className="text-xs font-normal" style={{ color: C.textMuted }}>/{ramTotalGb} GB</span>
          </span>
        </div>
        <div className="h-0.5 rounded-full overflow-hidden" style={{ background: C.border }}>
          <div className="h-full rounded-full" style={barStyle(ramPct)} />
        </div>
      </div>

      {/* Temp */}
      <div className="md:flex-1 md:min-w-[7.5rem] px-3 md:px-5 py-3">
        <div className="flex items-baseline justify-between mb-1.5">
          <span className="text-xs" style={{ color: C.textMuted }}>Temp</span>
          <span className="text-sm font-semibold tabular-nums whitespace-nowrap" style={{ color: C.textPrimary }}>
            {data.gpu_temp_c != null ? `${data.gpu_temp_c}°C` : "—"}
          </span>
        </div>
        <div className="h-0.5 rounded-full" style={{ background: C.border }} />
      </div>
      </div>
    </div>
  );
}

export function HostMetricsBar() {
  const { data: hosts } = useQuery<Host[]>({
    queryKey: ["hosts"],
    queryFn: api.hosts.list,
  });

  // local hosts have no live metrics (spec: metrics endpoint returns empty) —
  // rendering an empty skeleton for them would be noise.
  const metricHosts = (hosts ?? []).filter((h) => h.enabled && h.kind !== "local");
  if (metricHosts.length === 0) return null;

  return (
    <div className="flex flex-col gap-2 mb-6">
      {metricHosts.map((h) => (
        <SingleHostMetricsBar key={h.id} host={h} />
      ))}
    </div>
  );
}

// ── Host Form Modal (admin-only) ──────────────────────────────────────────────

const EMPTY_FORM: HostCreate = {
  slug: "",
  display_name: "",
  kind: "ssh",
  ssh_host: "",
  ssh_user: "",
  ssh_key_path: "",
  control_url: "",
  wol_mac_address: "",
  power_managed: false,
  notes: "",
  enabled: true,
};

function hostToForm(host: Host): HostCreate {
  return {
    slug: host.slug,
    display_name: host.display_name,
    kind: host.kind,
    ssh_host: host.ssh_host ?? "",
    ssh_user: host.ssh_user ?? "",
    ssh_key_path: host.ssh_key_path ?? "",
    control_url: host.control_url ?? "",
    wol_mac_address: host.wol_mac_address ?? "",
    power_managed: host.power_managed,
    notes: host.notes ?? "",
    enabled: host.enabled,
  };
}

/** Trim string fields; empty strings become null so the API clears them. */
function formToPayload(form: HostCreate): HostCreate {
  const norm = (v: string | null | undefined) => {
    const t = (v ?? "").trim();
    return t.length > 0 ? t : null;
  };
  return {
    slug: form.slug.trim(),
    display_name: form.display_name.trim(),
    kind: form.kind,
    ssh_host: norm(form.ssh_host),
    ssh_user: norm(form.ssh_user),
    ssh_key_path: norm(form.ssh_key_path),
    control_url: norm(form.control_url),
    wol_mac_address: norm(form.wol_mac_address),
    power_managed: form.power_managed ?? false,
    notes: norm(form.notes),
    enabled: form.enabled ?? true,
  };
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  mono,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  mono?: boolean;
}) {
  const id = `host-field-${label.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-xs" style={{ color: C.textMuted }}>
        {label}
      </label>
      <input
        id={id}
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className={`text-sm px-3 py-2 rounded-lg outline-none ${mono ? "font-mono" : ""}`}
        style={{
          background: C.border,
          border: `1px solid ${C.borderSubtle}`,
          color: C.textPrimary,
        }}
      />
    </div>
  );
}

function HostFormModal({
  host,
  onClose,
}: {
  /** null = create, Host = edit */
  host: Host | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<HostCreate>(host ? hostToForm(host) : EMPTY_FORM);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const set = <K extends keyof HostCreate>(key: K, value: HostCreate[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["hosts"] });
    queryClient.invalidateQueries({ queryKey: ["host-metrics"] });
    queryClient.invalidateQueries({ queryKey: ["runtimes"] });
  };

  const saveMutation = useMutation({
    mutationFn: () => {
      const payload = formToPayload(form);
      return host ? api.hosts.update(host.id, payload) : api.hosts.create(payload);
    },
    onSuccess: () => {
      invalidate();
      onClose();
    },
    onError: (err) => setErrorMsg(extractApiError(err)),
  });

  const canSave = form.slug.trim().length > 0 && form.display_name.trim().length > 0;

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.15 }}
        className="fixed inset-0 z-40 flex items-end sm:items-center justify-center sm:p-4 bg-black/60"
        style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
        onClick={onClose}
      >
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
          {/* Header */}
          <div
            className="flex items-center justify-between p-5 border-b shrink-0"
            style={{ borderColor: "rgba(255,255,255,0.06)" }}
          >
            <h2 className="text-sm font-semibold" style={{ color: C.textPrimary }}>
              {host ? `Host bearbeiten — ${host.display_name}` : "Host hinzufügen"}
            </h2>
            <button
              onClick={onClose}
              aria-label="Schliessen"
              className="p-1 rounded-md hover:bg-[rgba(255,255,255,0.06)] cursor-pointer"
            >
              <X size={14} style={{ color: C.textMuted }} />
            </button>
          </div>

          {/* Body */}
          <div className="flex-1 overflow-y-auto p-5 flex flex-col gap-3">
            <Field label="Slug" value={form.slug} onChange={(v) => set("slug", v)} placeholder="z.B. dgx-spark" mono />
            <Field label="Anzeigename" value={form.display_name} onChange={(v) => set("display_name", v)} placeholder="z.B. DGX Spark" />

            {/* Kind pills */}
            <div className="flex flex-col gap-1">
              <span className="text-xs" style={{ color: C.textMuted }}>Typ</span>
              <div className="flex gap-1.5">
                {(Object.keys(KIND_LABEL) as HostKind[]).map((k) => {
                  const active = form.kind === k;
                  return (
                    <button
                      key={k}
                      onClick={() => set("kind", k)}
                      className="text-xs px-2.5 py-1 rounded-md cursor-pointer transition-all"
                      style={{
                        background: active ? C.accentSubtle : C.borderSubtle,
                        border: `1px solid ${active ? C.borderAccent : C.border}`,
                        color: active ? C.accent : C.textMuted,
                        fontWeight: active ? 600 : 400,
                      }}
                    >
                      {KIND_LABEL[k]}
                    </button>
                  );
                })}
              </div>
            </div>

            {form.kind === "ssh" && (
              <>
                <Field label="SSH Host" value={form.ssh_host ?? ""} onChange={(v) => set("ssh_host", v)} placeholder="IP oder Hostname (z.B. 192.0.2.10)" mono />
                <Field label="SSH User" value={form.ssh_user ?? ""} onChange={(v) => set("ssh_user", v)} mono />
                <Field label="SSH Key Pfad" value={form.ssh_key_path ?? ""} onChange={(v) => set("ssh_key_path", v)} placeholder="/root/.ssh/id_ed25519" mono />
              </>
            )}

            {form.kind === "flask_wol" && (
              <>
                <Field label="Control URL" value={form.control_url ?? ""} onChange={(v) => set("control_url", v)} placeholder="http://192.0.2.20:5555" mono />
                <Field label="WoL MAC-Adresse" value={form.wol_mac_address ?? ""} onChange={(v) => set("wol_mac_address", v)} placeholder="00:00:5E:00:53:01" mono />
              </>
            )}

            {form.kind !== "local" && (
              <label className="flex items-center gap-2 text-xs cursor-pointer" style={{ color: C.textMuted }}>
                <input
                  type="checkbox"
                  checked={form.power_managed ?? false}
                  onChange={(e) => set("power_managed", e.target.checked)}
                  style={{ accentColor: C.accent }}
                />
                Power-managed (Box schläft bei Inaktivität)
              </label>
            )}

            <div className="flex flex-col gap-1">
              <label htmlFor="host-field-notes" className="text-xs" style={{ color: C.textMuted }}>
                Notizen (GPU-Profil, Eigenheiten)
              </label>
              <textarea
                id="host-field-notes"
                value={form.notes ?? ""}
                onChange={(e) => set("notes", e.target.value)}
                rows={2}
                className="text-sm px-3 py-2 rounded-lg outline-none resize-none"
                style={{
                  background: C.border,
                  border: `1px solid ${C.borderSubtle}`,
                  color: C.textPrimary,
                }}
              />
            </div>

            <label className="flex items-center gap-2 text-xs cursor-pointer" style={{ color: C.textMuted }}>
              <input
                type="checkbox"
                checked={form.enabled ?? true}
                onChange={(e) => set("enabled", e.target.checked)}
                style={{ accentColor: C.accent }}
              />
              Aktiviert
            </label>

            {errorMsg && (
              <div
                className="text-xs px-3 py-2 rounded-lg"
                style={{
                  background: `${C.error}14`,
                  border: `1px solid ${C.error}33`,
                  color: STATUS_TEXT.error,
                }}
              >
                {errorMsg}
              </div>
            )}
          </div>

          {/* Footer */}
          <div
            className="flex items-center justify-end gap-2 px-5 py-3 border-t shrink-0"
            style={{
              borderColor: "rgba(255,255,255,0.06)",
              paddingBottom: "calc(env(safe-area-inset-bottom) + 0.75rem)",
            }}
          >
            <button
              onClick={onClose}
              className="text-xs px-3 py-1.5 rounded-lg cursor-pointer"
              style={{ color: C.textMuted, border: `1px solid ${C.borderSubtle}`, background: C.borderSubtle }}
            >
              Abbrechen
            </button>
            <button
              onClick={() => saveMutation.mutate()}
              disabled={!canSave || saveMutation.isPending}
              className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
              style={{
                background: C.accentSubtle,
                border: `1px solid ${C.borderAccent}`,
                color: C.accent,
              }}
            >
              {saveMutation.isPending && <Loader2 size={11} className="animate-spin" />}
              {host ? "Speichern" : "Hinzufügen"}
            </button>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}

// ── Host Card ─────────────────────────────────────────────────────────────────

function HostCard({
  host,
  boundCount,
  isAdmin,
  onEdit,
  onDelete,
  deletePending,
}: {
  host: Host;
  boundCount: number;
  isAdmin: boolean;
  onEdit: () => void;
  onDelete: () => void;
  deletePending: boolean;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, x: -4 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
      className="flex flex-col gap-2 px-3 py-2.5 sm:flex-row sm:items-center sm:gap-3"
      style={{
        background: C.borderSubtle,
        border: `1px solid ${C.borderSubtle}`,
        borderRadius: "10px",
      }}
    >
      <div className="flex items-center gap-3 min-w-0 sm:flex-1">
        {/* Status dot — enabled/disabled */}
        <div
          className="w-1.5 h-1.5 rounded-full shrink-0"
          style={{ background: host.enabled ? C.online : STATUS.offline }}
        />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-medium text-sm truncate" style={{ color: C.textPrimary }}>
              {host.display_name}
            </span>
            <span
              className="shrink-0 uppercase"
              style={{
                background: C.border,
                color: C.textMuted,
                fontSize: "9px",
                padding: "1px 5px",
                borderRadius: "4px",
                letterSpacing: "0.06em",
              }}
            >
              {KIND_LABEL[host.kind]}
            </span>
          </div>
          <div className="flex items-center gap-1.5 mt-0.5 flex-wrap">
            <span className="text-xs font-mono" style={{ color: C.textMuted }}>
              {host.slug}
            </span>
            <span style={{ color: C.borderSubtle }}>·</span>
            <span className="text-xs" style={{ color: host.enabled ? C.textMuted : C.textDim }}>
              {host.enabled ? "Aktiv" : "Deaktiviert"}
            </span>
            <span style={{ color: C.borderSubtle }}>·</span>
            <span className="text-xs tabular-nums" style={{ color: boundCount > 0 ? C.textSecondary : C.textMuted }}>
              {boundCount} Runtime{boundCount === 1 ? "" : "s"}
            </span>
            {host.kind === "ssh" && host.ssh_host && (
              <>
                <span style={{ color: C.borderSubtle }}>·</span>
                <span className="text-xs font-mono truncate" style={{ color: C.textDim }}>
                  {host.ssh_host}
                </span>
              </>
            )}
            {host.kind === "flask_wol" && host.control_url && (
              <>
                <span style={{ color: C.borderSubtle }}>·</span>
                <span className="text-xs font-mono truncate" style={{ color: C.textDim }}>
                  {host.control_url}
                </span>
              </>
            )}
          </div>
        </div>
      </div>

      {isAdmin && (
        <div className="flex items-center gap-1 shrink-0 self-end sm:self-auto">
          <button
            onClick={onEdit}
            title="Bearbeiten"
            aria-label={`Host ${host.display_name} bearbeiten`}
            className="flex items-center justify-center w-7 h-7 rounded-lg transition-all cursor-pointer"
            style={{
              background: C.borderSubtle,
              border: `1px solid ${C.borderSubtle}`,
              color: C.textMuted,
            }}
          >
            <Pencil size={12} />
          </button>
          <button
            onClick={onDelete}
            disabled={deletePending}
            title="Löschen"
            aria-label={`Host ${host.display_name} löschen`}
            className="flex items-center justify-center w-7 h-7 rounded-lg transition-all cursor-pointer disabled:cursor-not-allowed"
            style={{
              background: `${C.error}14`,
              border: `1px solid ${C.error}33`,
              color: STATUS_TEXT.error,
            }}
          >
            {deletePending ? <Loader2 size={12} className="animate-spin" /> : <Trash2 size={12} />}
          </button>
        </div>
      )}
    </motion.div>
  );
}

// ── Hosts Section ─────────────────────────────────────────────────────────────

export function HostsSection() {
  const queryClient = useQueryClient();
  const currentUser = useAppStore((s) => s.currentUser);
  const isAdmin = currentUser?.role === "admin";

  // modal: undefined = closed, null = create, Host = edit
  const [modalHost, setModalHost] = useState<Host | null | undefined>(undefined);
  const [feedback, setFeedback] = useState<string | null>(null);

  const { data: hosts, isLoading } = useQuery<Host[]>({
    queryKey: ["hosts"],
    queryFn: api.hosts.list,
  });

  const { data: runtimesData } = useQuery({
    queryKey: ["runtimes"],
    queryFn: () => api.runtimes.list(),
  });

  const boundCount = (hostId: string) =>
    runtimesData?.runtimes.filter((rt) => rt.host?.id === hostId).length ?? 0;

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.hosts.delete(id),
    onSuccess: () => {
      setFeedback(null);
      queryClient.invalidateQueries({ queryKey: ["hosts"] });
      queryClient.invalidateQueries({ queryKey: ["host-metrics"] });
      queryClient.invalidateQueries({ queryKey: ["runtimes"] });
    },
    // 409 = runtimes still bound (guard) — show the backend message instead
    // of silently failing.
    onError: (err) => setFeedback(extractApiError(err)),
  });

  return (
    <div className="mt-8">
      {/* Section header — matches vLLM/LM Studio section style */}
      <div className="flex items-center gap-3 mb-4">
        <div
          className="w-px"
          style={{
            alignSelf: "stretch",
            background: `linear-gradient(to bottom, ${C.textDim} 0%, transparent 100%)`,
            minHeight: "36px",
          }}
        />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="text-sm font-semibold" style={{ color: C.textPrimary }}>Hosts</h2>
            <span
              className="text-xs px-1.5 py-px rounded"
              style={{ color: C.textMuted, background: C.border, fontSize: "10px", letterSpacing: "0.06em" }}
            >
              Registry
            </span>
          </div>
          <p className="text-xs mt-0.5" style={{ color: C.textMuted }}>
            Physische Boxen, auf denen LLM-Runtimes laufen
          </p>
        </div>
        {isAdmin && (
          <button
            onClick={() => setModalHost(null)}
            className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg transition-all cursor-pointer shrink-0"
            style={{
              background: C.accentSubtle,
              border: `1px solid ${C.borderAccent}`,
              color: C.accent,
            }}
          >
            <Plus size={11} />
            Host
          </button>
        )}
      </div>

      {isLoading && (
        <div className="flex items-center gap-2 py-2" style={{ color: C.textMuted }}>
          <Loader2 size={13} className="animate-spin" />
          <span className="text-xs">Lade Hosts...</span>
        </div>
      )}

      {feedback && (
        <div
          className="flex items-center justify-between gap-3 text-xs px-4 py-3 mb-3 rounded-xl"
          style={{
            color: STATUS_TEXT.error,
            background: `${C.error}0F`,
            border: `1px solid ${C.error}26`,
          }}
        >
          <span>{feedback}</span>
          <button
            onClick={() => setFeedback(null)}
            aria-label="Meldung schliessen"
            className="cursor-pointer shrink-0"
            style={{ color: STATUS_TEXT.error }}
          >
            <X size={12} />
          </button>
        </div>
      )}

      {!isLoading && (hosts ?? []).length === 0 && (
        <div className="flex items-center gap-2 text-xs py-6 justify-center" style={{ color: C.textMuted }}>
          <Server size={13} />
          Keine Hosts registriert — Cloud-Runtimes brauchen keinen.
        </div>
      )}

      <div className="flex flex-col gap-2">
        {(hosts ?? []).map((h) => (
          <HostCard
            key={h.id}
            host={h}
            boundCount={boundCount(h.id)}
            isAdmin={isAdmin}
            onEdit={() => setModalHost(h)}
            onDelete={() => deleteMutation.mutate(h.id)}
            deletePending={deleteMutation.isPending && deleteMutation.variables === h.id}
          />
        ))}
      </div>

      {modalHost !== undefined && (
        <HostFormModal host={modalHost} onClose={() => setModalHost(undefined)} />
      )}
    </div>
  );
}

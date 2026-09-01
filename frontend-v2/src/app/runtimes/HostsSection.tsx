"use client";

/**
 * Host Registry UI (ADR-048) — generische Multi-Host Control-Plane.
 *
 * HostsSection: Cards (Name, Kind-Badge, Status, gebundene Runtimes) +
 * Add/Edit-Modal (admin-only) + Delete mit 409-Guard-Feedback.
 *
 * Neu anlegen läuft über EINEN Knopf „Gerät hinzufügen" (AddDeviceDialog):
 * der fragt nach der Situation des Operators und öffnet dann den passenden
 * der vier bestehenden Wege (Onboarding / Pairing / BoxWizard / Formular).
 *
 * Metrics-bar-free (slot-stage redesign): live GPU/RAM/temp bars now live in
 * SlotStage's TelemetryColumn, not here — see docs/plans/2026-08-13
 * -runtimes-slot-stage-design.md.
 */

import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Pencil, Plus, Server, Trash2, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { api } from "@/lib/api";
import type { Host, HostCreate, HostKind } from "@/lib/types";
import { useAppStore } from "@/lib/store";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { C, STATUS_TEXT } from "@/lib/colors";
import { BoxWizard } from "./BoxWizard";
import { HostOnboardDialog } from "./HostOnboardDialog";
import { NodePairingDialog } from "./NodePairingDialog";
import { AddDeviceDialog, type DeviceRoute } from "./AddDeviceDialog";
import { Section, SectionOrFragment } from "@/components/shared/Section";
import { ListRow, MetaChip, MetaText } from "@/components/shared/ListRow";
import { OverflowMenu } from "@/components/shared/OverflowMenu";

// ── Helpers ───────────────────────────────────────────────────────────────────

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

// labelKey pattern (docs/i18n.md): resolved via t() at the render site.
const KIND_LABEL_KEY: Record<HostKind, string> = {
  ssh: "kindSsh",
  flask_wol: "kindFlaskWol",
  local: "kindLocal",
  agent: "kindAgent",
};

/**
 * Typen, die man im Formular von Hand wählen darf. `agent` fehlt bewusst:
 * ein manuell angelegter kind=agent-Host hat keinen agent_token_hash
 * (den vergibt nur routers/nodes.py beim Einlösen eines Pairing-Codes),
 * kann sich also nie melden und bleibt für immer grau — eine Falle.
 * Bestehende agent-Zeilen bleiben unangetastet (Edit zeigt den Typ gesperrt).
 */
const SELECTABLE_KINDS: readonly HostKind[] = ["ssh", "flask_wol", "local"];

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
  onOpenPairing,
}: {
  /** null = create, Host = edit */
  host: Host | null;
  onClose: () => void;
  /** Sprung zum Pairing-Weg aus dem Typ-Hinweis (nur beim Anlegen sinnvoll). */
  onOpenPairing?: () => void;
}) {
  const t = useTranslations("runtimes.hosts");
  const queryClient = useQueryClient();
  const [form, setForm] = useState<HostCreate>(host ? hostToForm(host) : EMPTY_FORM);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // iOS-safe scroll lock + Esc close (panel register rule 4)
  useBodyScrollLock(true);
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  const set = <K extends keyof HostCreate>(key: K, value: HostCreate[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["hosts"] });
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
            border: "1px solid var(--color-border)",
            boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
          }}
        >
          {/* Header */}
          <div
            className="flex items-center justify-between p-5 border-b shrink-0"
            style={{ borderColor: "var(--color-border)" }}
          >
            <h2 className="text-sm font-semibold" style={{ color: C.textPrimary }}>
              {host ? t("editHostTitle", { name: host.display_name }) : t("addHostTitle")}
            </h2>
            <button
              onClick={onClose}
              aria-label={t("close")}
              className="p-1 rounded-md hover:bg-[var(--color-bg-hover)] cursor-pointer"
            >
              <X size={14} style={{ color: C.textMuted }} />
            </button>
          </div>

          {/* Body */}
          <div className="flex-1 overflow-y-auto p-5 flex flex-col gap-3">
            <Field label={t("fieldSlug")} value={form.slug} onChange={(v) => set("slug", v)} placeholder={t("slugPlaceholder")} mono />
            <Field label={t("fieldDisplayName")} value={form.display_name} onChange={(v) => set("display_name", v)} placeholder={t("displayNamePlaceholder")} />

            {/* Kind pills — `agent` ist nicht wählbar (siehe SELECTABLE_KINDS).
                Eine bestehende agent-Zeile zeigt ihren Typ gesperrt an. */}
            <div className="flex flex-col gap-1">
              <span className="text-xs" style={{ color: C.textMuted }}>{t("fieldType")}</span>
              {form.kind === "agent" ? (
                <span
                  data-testid="host-kind-locked"
                  className="self-start text-xs px-2.5 py-1 rounded-md font-semibold"
                  style={{
                    background: C.accentSubtle,
                    border: `1px solid ${C.borderAccent}`,
                    color: C.accent,
                  }}
                >
                  {t(KIND_LABEL_KEY.agent)} · {t("kindAgentLocked")}
                </span>
              ) : (
                <div className="flex gap-1.5">
                  {SELECTABLE_KINDS.map((k) => {
                    const active = form.kind === k;
                    return (
                      <button
                        key={k}
                        type="button"
                        onClick={() => set("kind", k)}
                        className="text-xs px-2.5 py-1 rounded-md cursor-pointer transition-all"
                        style={{
                          background: active ? C.accentSubtle : C.borderSubtle,
                          border: `1px solid ${active ? C.borderAccent : C.border}`,
                          color: active ? C.accent : C.textMuted,
                          fontWeight: active ? 600 : 400,
                        }}
                      >
                        {t(KIND_LABEL_KEY[k])}
                      </button>
                    );
                  })}
                </div>
              )}
              {!host && (
                <p className="text-[11px] leading-relaxed" style={{ color: C.textMuted }}>
                  {t("kindAgentHint")}{" "}
                  {onOpenPairing && (
                    <button
                      type="button"
                      data-testid="host-kind-agent-hint-link"
                      onClick={onOpenPairing}
                      className="underline underline-offset-2 cursor-pointer hover:text-[var(--color-text-primary)]"
                      style={{ color: C.textSecondary }}
                    >
                      {t("kindAgentHintLink")}
                    </button>
                  )}
                </p>
              )}
            </div>

            {form.kind === "ssh" && (
              <>
                <Field label={t("fieldSshHost")} value={form.ssh_host ?? ""} onChange={(v) => set("ssh_host", v)} placeholder={t("sshHostPlaceholder")} mono />
                <Field label={t("fieldSshUser")} value={form.ssh_user ?? ""} onChange={(v) => set("ssh_user", v)} mono />
                <Field label={t("fieldSshKeyPath")} value={form.ssh_key_path ?? ""} onChange={(v) => set("ssh_key_path", v)} placeholder="/root/.ssh/id_ed25519" mono />
              </>
            )}

            {form.kind === "flask_wol" && (
              <>
                <Field label={t("fieldControlUrl")} value={form.control_url ?? ""} onChange={(v) => set("control_url", v)} placeholder="http://192.0.2.20:5555" mono />
                <Field label={t("fieldWolMac")} value={form.wol_mac_address ?? ""} onChange={(v) => set("wol_mac_address", v)} placeholder="00:00:5E:00:53:01" mono />
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
                {t("powerManaged")}
              </label>
            )}

            <div className="flex flex-col gap-1">
              <label htmlFor="host-field-notes" className="text-xs" style={{ color: C.textMuted }}>
                {t("fieldNotes")}
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
              {t("enabled")}
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
              borderColor: "var(--color-border)",
              paddingBottom: "calc(env(safe-area-inset-bottom) + 0.75rem)",
            }}
          >
            <button
              onClick={onClose}
              className="text-xs px-3 py-1.5 rounded-lg cursor-pointer"
              style={{ color: C.textMuted, border: `1px solid ${C.borderSubtle}`, background: C.borderSubtle }}
            >
              {t("cancel")}
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
              {host ? t("save") : t("add")}
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
  const t = useTranslations("runtimes.hosts");
  return (
    <ListRow
      testId="host-row"
      dataAttrs={{ "data-slug": host.slug }}
      tone={host.enabled ? "ok" : "idle"}
      muted={!host.enabled}
      name={host.display_name}
      summary={[
        host.enabled ? t("active") : t("disabled"),
        t(KIND_LABEL_KEY[host.kind]),
        `${boundCount} ${boundCount === 1 ? t("runtimeSingular") : t("runtimePlural")}`,
      ].join(" · ")}
      chips={
        <>
          <MetaChip tone={host.enabled ? "ok" : "idle"}>
            {host.enabled ? t("active") : t("disabled")}
          </MetaChip>
          <MetaChip tone="idle">{t(KIND_LABEL_KEY[host.kind])}</MetaChip>
          <MetaChip tone="idle" className="tabular-nums">
            {boundCount} {boundCount === 1 ? t("runtimeSingular") : t("runtimePlural")}
          </MetaChip>
        </>
      }
      meta={
        <>
          <MetaText mono>{host.slug}</MetaText>
          {host.kind === "ssh" && host.ssh_host && (
            <MetaText mono title={host.ssh_host}>
              {host.ssh_host}
            </MetaText>
          )}
          {host.kind === "flask_wol" && host.control_url && (
            <MetaText mono title={host.control_url}>
              {host.control_url}
            </MetaText>
          )}
        </>
      }
      action={
        isAdmin ? (
          <button
            onClick={onEdit}
            title={t("edit")}
            aria-label={t("editHostAria", { name: host.display_name })}
            className="flex items-center justify-center w-11 h-11 sm:w-7 sm:h-7 min-w-11 sm:min-w-[28px] cursor-pointer"
          >
            <span
              aria-hidden
              className="action-btn flex items-center justify-center w-7 h-7 rounded-md transition-colors"
              style={{
                background: "transparent",
                border: `1px solid ${C.borderActive}`,
                color: C.textMuted,
                ["--action-hover" as string]: C.bgHover,
              }}
            >
              <Pencil size={12} />
            </span>
          </button>
        ) : undefined
      }
      overflow={
        isAdmin ? (
          <OverflowMenu
            label={t("rowActions", { name: host.display_name })}
            testId={`host-more-${host.slug}`}
            actions={[
              {
                id: "delete",
                label: t("delete"),
                icon: Trash2,
                destructive: true,
                loading: deletePending,
                onClick: onDelete,
              },
            ]}
          />
        ) : undefined
      }
    />
  );
}

// ── Hosts Section ─────────────────────────────────────────────────────────────

export function HostsSection({ embedded = false }: { embedded?: boolean } = {}) {
  const t = useTranslations("runtimes.hosts");
  const queryClient = useQueryClient();
  const currentUser = useAppStore((s) => s.currentUser);
  const isAdmin = currentUser?.role === "admin";

  // modal: undefined = closed, null = create, Host = edit
  const [modalHost, setModalHost] = useState<Host | null | undefined>(undefined);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [chooserOpen, setChooserOpen] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [onboardOpen, setOnboardOpen] = useState(false);
  const [pairingOpen, setPairingOpen] = useState(false);

  /**
   * Routing „Situation → Weg". Der Chooser kennt keine Dialoge, nur Routen —
   * hier wird entschieden, was aufgeht. Spätere Verkettung (Onboarding →
   * Modell-Schritte des Wizards) hängt sich an den `onboard`-Zweig bzw. an
   * ein Fertig-Signal des HostOnboardDialog, ohne den Chooser anzufassen.
   */
  const openRoute = (route: DeviceRoute) => {
    setChooserOpen(false);
    switch (route) {
      case "onboard": setOnboardOpen(true); break;
      case "pairing": setPairingOpen(true); break;
      case "wizard": setWizardOpen(true); break;
      case "manual": setModalHost(null); break;
    }
  };

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
      queryClient.invalidateQueries({ queryKey: ["runtimes"] });
    },
    // 409 = runtimes still bound (guard) — show the backend message instead
    // of silently failing.
    onError: (err) => setFeedback(extractApiError(err)),
  });

  return (
    <SectionOrFragment
      embedded={embedded}
      id="hosts"
      title={t("title")}
      hint={t("subtitle")}
      count={(hosts ?? []).length}
      actions={
        isAdmin ? (
          /* Ein Einstieg für alle vier Wege — die Wahl (Onboarding / Pairing /
             Wizard / Formular) trifft der AddDeviceDialog nach Situation. */
          <button
            data-testid="hosts-add-device"
            onClick={() => setChooserOpen(true)}
            className="flex items-center gap-1.5 text-xs px-3 py-2 sm:py-1.5 min-h-11 sm:min-h-0 rounded-md transition-all cursor-pointer"
            style={{
              background: C.accentSubtle,
              border: `1px solid ${C.borderAccent}`,
              color: C.accent,
            }}
          >
            <Plus size={11} />
            {t("addDeviceButton")}
          </button>
        ) : undefined
      }
    >

      {isLoading && (
        <div className="flex items-center gap-2 py-2" style={{ color: C.textMuted }}>
          <Loader2 size={13} className="animate-spin" />
          <span className="text-xs">{t("loading")}</span>
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
            aria-label={t("dismissMessage")}
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
          {t("noHostsRegistered")}
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
        <HostFormModal
          host={modalHost}
          onClose={() => setModalHost(undefined)}
          onOpenPairing={() => {
            setModalHost(undefined);
            setPairingOpen(true);
          }}
        />
      )}

      <AddDeviceDialog open={chooserOpen} onClose={() => setChooserOpen(false)} onChoose={openRoute} />

      {wizardOpen && <BoxWizard onClose={() => setWizardOpen(false)} />}
      <HostOnboardDialog open={onboardOpen} onClose={() => setOnboardOpen(false)} />
      <NodePairingDialog open={pairingOpen} onClose={() => setPairingOpen(false)} />
    </SectionOrFragment>
  );
}

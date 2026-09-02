"use client";

/**
 * HostOnboardDialog — "Gerät automatisch einrichten" (Fleet & Rezepte v2, Phase 2).
 *
 * Marks Zielbild: IP + Username + Passwort (oder Key/vorhandenes Credential)
 * rein, MC macht den Rest selbst (services/host_onboarding.py) — verbinden,
 * eigenen Key hinterlassen, Vault, optional Bootstrap + node-agent. Nie
 * SSH von Hand.
 *
 * Zwei Phasen wie SshProcessDeployDialog: Formular → Klick "Starten" →
 * job_id → Live-Log-Stream (cursor-basiertes Polling, gleiches Muster).
 * Modal-Chrome ist ResponsiveModal (review finding #10, 30.08.2026 — 9
 * andere Dialoge nutzen es bereits, kein Grund für eine zehnte
 * handgebaute Variante) — bewusst funktional schlicht, das
 * Geschmacks-Feintuning ist Marks Abnahme.
 */

import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { AlertTriangle, CheckCircle2, Loader2, X } from "lucide-react";
import { api } from "@/lib/api";
import { RoleField, suggestRole } from "./RoleField";
import type {
  Host,
  HostRole,
  Credential,
  HostBootstrapLogLine,
  HostOnboardStatus,
} from "@/lib/types";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { extractApiError } from "@/components/shared/SshProcessDeployDialog";
import { C, STATUS_TEXT } from "@/lib/colors";

type AuthMethod = "password" | "private_key" | "existing_credential";

const STATUS_LABEL_KEY: Partial<Record<HostOnboardStatus, string>> = {
  done: "onboardStatusDone",
  failed: "onboardStatusFailed",
  auth_failed: "onboardStatusAuthFailed",
  unreachable: "onboardStatusUnreachable",
  needs_sudo: "onboardStatusNeedsSudo",
};

const TERMINAL_STATUSES = new Set<HostOnboardStatus>([
  "done", "failed", "auth_failed", "unreachable", "needs_sudo",
]);

export function HostOnboardDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useTranslations("runtimes.hosts");
  const queryClient = useQueryClient();

  const [address, setAddress] = useState("");
  const [username, setUsername] = useState("");
  const [authMethod, setAuthMethod] = useState<AuthMethod>("password");
  const [password, setPassword] = useState("");
  const [privateKey, setPrivateKey] = useState("");
  const [existingCredentialId, setExistingCredentialId] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [bootstrap, setBootstrap] = useState(true);
  const [installAgent, setInstallAgent] = useState(true);
  // Rolle (P2): Vorschlag aus dem Bestand — erste Box Head, weitere Worker.
  const [role, setRole] = useState<HostRole | null>(null);
  const [roleSuggested, setRoleSuggested] = useState(true);

  const [jobId, setJobId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [log, setLog] = useState<HostBootstrapLogLine[]>([]);
  const [status, setStatus] = useState<HostOnboardStatus>("idle");
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const cursor = useRef(0);
  const logEnd = useRef<HTMLDivElement | null>(null);

  // Bestand für den Rollen-Vorschlag; Handklick (roleSuggested=false) gewinnt.
  const { data: existingHosts } = useQuery<Host[]>({ queryKey: ["hosts"], queryFn: api.hosts.list, enabled: open });
  useEffect(() => {
    if (existingHosts === undefined || !roleSuggested) return;
    setRole(suggestRole(existingHosts.length));
  }, [existingHosts, roleSuggested]);

  const credentialsQuery = useQuery({
    queryKey: ["credentials"],
    queryFn: api.credentials.list,
    enabled: authMethod === "existing_credential",
  });
  const sshKeyCredentials: Credential[] = (credentialsQuery.data ?? []).filter(
    (c) => c.credential_type === "ssh_key",
  );

  // ResponsiveModal keeps this component mounted across opens (needed for
  // its close animation) — this used to happen "for free" via the parent
  // mounting/unmounting the whole dialog on every open (review finding #10,
  // 30.08.2026). Reset explicitly instead, on every open.
  useEffect(() => {
    if (!open) return;
    setAddress(""); setUsername(""); setAuthMethod("password"); setPassword("");
    setPrivateKey(""); setExistingCredentialId(""); setDisplayName("");
    setBootstrap(true); setInstallAgent(true);
    setJobId(null); setBusy(false); setError(null); setLog([]);
    setStatus("idle"); setStatusMessage(null);
    cursor.current = 0;
  }, [open]);

  const running = status === "running";

  useEffect(() => {
    if (!open || !jobId || !running) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const res = await api.hosts.onboardLog(jobId, cursor.current);
        if (cancelled) return;
        cursor.current = res.cursor;
        if (res.lines.length > 0) setLog((prev) => [...prev, ...res.lines]);
        setStatus(res.status);
        setStatusMessage(res.message);
      } catch (err) {
        if (!cancelled) setError(extractApiError(err));
      }
    };
    poll();
    const timer = setInterval(poll, 2000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [open, jobId, running]);

  useEffect(() => {
    logEnd.current?.scrollIntoView?.({ block: "nearest" });
  }, [log.length]);

  useEffect(() => {
    if (TERMINAL_STATUSES.has(status)) {
      queryClient.invalidateQueries({ queryKey: ["hosts"] });
    }
  }, [status, queryClient]);

  const canStart =
    address.trim().length > 0 &&
    username.trim().length > 0 &&
    (authMethod === "password" ? password.length > 0
      : authMethod === "private_key" ? privateKey.trim().length > 0
      : existingCredentialId.length > 0);

  async function start() {
    setBusy(true);
    setError(null);
    setLog([]);
    cursor.current = 0;
    setStatus("idle");
    setStatusMessage(null);
    try {
      const auth =
        authMethod === "password" ? { password }
        : authMethod === "private_key" ? { private_key: privateKey }
        : { use_existing_credential_id: existingCredentialId };
      const res = await api.hosts.onboard({
        address: address.trim(),
        username: username.trim(),
        auth,
        display_name: displayName.trim() || null,
        bootstrap,
        install_agent: installAgent,
        role: role ?? suggestRole(existingHosts?.length ?? 0),
      });
      setJobId(res.job_id);
      setStatus("running");
    } catch (err) {
      setError(extractApiError(err));
    } finally {
      setBusy(false);
    }
  }

  const statusLabelKey = STATUS_LABEL_KEY[status];
  const isError = status === "failed" || status === "auth_failed" || status === "unreachable" || status === "needs_sudo";
  const formDisabled = running || TERMINAL_STATUSES.has(status);

  return (
    <ResponsiveModal open={open} onClose={onClose} aria-labelledby="host-onboard-title">
      <div
        className="flex items-center justify-between p-5 border-b shrink-0"
        style={{ borderColor: "var(--color-border)" }}
      >
        <h2 id="host-onboard-title" className="text-sm font-semibold" style={{ color: C.textPrimary }}>
          {t("onboardDialogTitle")}
        </h2>
        <button
          onClick={onClose}
          aria-label={t("onboardClose")}
          className="p-1 rounded-md hover:bg-[var(--color-bg-hover)] cursor-pointer"
        >
          <X size={14} style={{ color: C.textMuted }} />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-5 flex flex-col gap-3">
        <p className="text-[11px]" style={{ color: C.textMuted }}>{t("onboardIntro")}</p>

        <div className="grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1">
            <span className="text-xs" style={{ color: C.textMuted }}>{t("onboardFieldAddress")}</span>
            <input
              type="text"
              value={address}
              onChange={(e) => setAddress(e.target.value)}
              placeholder={t("onboardAddressPlaceholder")}
              disabled={formDisabled}
              data-testid="onboard-address"
              className="text-sm px-3 py-2 rounded-lg outline-none font-mono disabled:opacity-50"
              style={{ background: C.border, border: `1px solid ${C.borderSubtle}`, color: C.textPrimary }}
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs" style={{ color: C.textMuted }}>{t("onboardFieldUsername")}</span>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              disabled={formDisabled}
              data-testid="onboard-username"
              className="text-sm px-3 py-2 rounded-lg outline-none font-mono disabled:opacity-50"
              style={{ background: C.border, border: `1px solid ${C.borderSubtle}`, color: C.textPrimary }}
            />
          </label>
        </div>

        <label className="flex flex-col gap-1">
          <span className="text-xs" style={{ color: C.textMuted }}>{t("onboardFieldDisplayNameOptional")}</span>
          <input
            type="text"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            disabled={formDisabled}
            className="text-sm px-3 py-2 rounded-lg outline-none disabled:opacity-50"
            style={{ background: C.border, border: `1px solid ${C.borderSubtle}`, color: C.textPrimary }}
          />
        </label>

        <RoleField
          idPrefix="onboard-role"
          value={role}
          onChange={(r) => { setRole(r); setRoleSuggested(false); }}
          labelClassName="text-xs text-[var(--color-text-muted)]"
          suggested={roleSuggested}
        />

        <div className="flex flex-col gap-1">
          <span className="text-xs" style={{ color: C.textMuted }}>{t("onboardAuthLabel")}</span>
          <div className="flex gap-1.5">
            {([
              ["password", "onboardAuthPassword"],
              ["private_key", "onboardAuthPrivateKey"],
              ["existing_credential", "onboardAuthExisting"],
            ] as const).map(([method, labelKey]) => {
              const active = authMethod === method;
              return (
                <button
                  key={method}
                  type="button"
                  disabled={formDisabled}
                  onClick={() => setAuthMethod(method)}
                  className="text-xs px-2.5 py-1 rounded-md cursor-pointer transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                  style={{
                    background: active ? C.accentSubtle : C.borderSubtle,
                    border: `1px solid ${active ? C.borderAccent : C.border}`,
                    color: active ? C.accent : C.textMuted,
                    fontWeight: active ? 600 : 400,
                  }}
                >
                  {t(labelKey)}
                </button>
              );
            })}
          </div>
        </div>

        {authMethod === "password" && (
          <label className="flex flex-col gap-1">
            <span className="text-xs" style={{ color: C.textMuted }}>{t("onboardFieldPassword")}</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={formDisabled}
              data-testid="onboard-password"
              autoComplete="new-password"
              className="text-sm px-3 py-2 rounded-lg outline-none font-mono disabled:opacity-50"
              style={{ background: C.border, border: `1px solid ${C.borderSubtle}`, color: C.textPrimary }}
            />
          </label>
        )}

        {authMethod === "private_key" && (
          <label className="flex flex-col gap-1">
            <span className="text-xs" style={{ color: C.textMuted }}>{t("onboardFieldPrivateKey")}</span>
            <textarea
              value={privateKey}
              onChange={(e) => setPrivateKey(e.target.value)}
              disabled={formDisabled}
              rows={4}
              data-testid="onboard-private-key"
              className="text-xs px-3 py-2 rounded-lg outline-none font-mono resize-none disabled:opacity-50"
              style={{ background: C.border, border: `1px solid ${C.borderSubtle}`, color: C.textPrimary }}
            />
          </label>
        )}

        {authMethod === "existing_credential" && (
          <label className="flex flex-col gap-1">
            <span className="text-xs" style={{ color: C.textMuted }}>{t("onboardFieldExistingCredential")}</span>
            {sshKeyCredentials.length > 0 ? (
              <select
                value={existingCredentialId}
                onChange={(e) => setExistingCredentialId(e.target.value)}
                disabled={formDisabled}
                className="rounded-lg text-sm px-3 py-2 outline-none disabled:opacity-50"
                style={{ background: C.border, border: `1px solid ${C.borderSubtle}`, color: C.textPrimary }}
              >
                <option value="" />
                {sshKeyCredentials.map((c) => (
                  <option key={c.id} value={c.id}>{c.name}</option>
                ))}
              </select>
            ) : (
              <span className="text-xs" style={{ color: C.warning }}>{t("onboardNoExistingCredentials")}</span>
            )}
          </label>
        )}

        <label className="flex items-center gap-2 text-xs cursor-pointer" style={{ color: C.textMuted }}>
          <input
            type="checkbox"
            checked={bootstrap}
            onChange={(e) => setBootstrap(e.target.checked)}
            disabled={formDisabled}
            style={{ accentColor: C.accent }}
          />
          {t("onboardBootstrapCheckbox")}
        </label>
        <label className="flex items-center gap-2 text-xs cursor-pointer" style={{ color: C.textMuted }}>
          <input
            type="checkbox"
            checked={installAgent}
            onChange={(e) => setInstallAgent(e.target.checked)}
            disabled={formDisabled}
            style={{ accentColor: C.accent }}
          />
          {t("onboardInstallAgentCheckbox")}
        </label>

        {error && (
          <div
            className="text-xs px-3 py-2 rounded-lg"
            style={{ background: `${C.error}14`, border: `1px solid ${C.error}33`, color: STATUS_TEXT.error }}
          >
            {error}
          </div>
        )}

        {log.length > 0 && (
          <div
            data-testid="onboard-log"
            className="rounded-md border px-2.5 py-2 max-h-56 overflow-y-auto font-mono text-[10px] flex flex-col gap-0.5"
            style={{ borderColor: C.borderSubtle, background: C.border }}
          >
            {log.map((line, i) => (
              <div
                key={i}
                className={`whitespace-pre-wrap break-words ${
                  line.level === "error" ? "text-err" : line.level === "warn" ? "text-warn" : ""
                }`}
                style={line.level === "info" ? { color: C.textMuted } : undefined}
              >
                {line.text}
              </div>
            ))}
            <div ref={logEnd} />
          </div>
        )}

        {statusLabelKey && (
          <div
            data-testid="onboard-status"
            className="flex items-start gap-1.5 text-[11px] px-3 py-2 rounded-lg"
            style={
              isError
                ? { background: `${C.error}14`, border: `1px solid ${C.error}33`, color: STATUS_TEXT.error }
                : { color: STATUS_TEXT.online }
            }
          >
            {isError ? <AlertTriangle size={12} className="shrink-0 mt-px" /> : <CheckCircle2 size={12} className="shrink-0 mt-px" />}
            <span>
              <strong>{t(statusLabelKey)}</strong>
              {statusMessage ? ` — ${statusMessage}` : ""}
            </span>
          </div>
        )}
      </div>

      <div
        className="flex items-center justify-end gap-2 px-5 py-3 border-t shrink-0"
        style={{ borderColor: "var(--color-border)", paddingBottom: "calc(env(safe-area-inset-bottom) + 0.75rem)" }}
      >
        {TERMINAL_STATUSES.has(status) ? (
          <button
            onClick={onClose}
            data-testid="onboard-done"
            className="text-xs px-3 py-1.5 rounded-lg cursor-pointer"
            style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.accent }}
          >
            {t("onboardDone")}
          </button>
        ) : (
          <>
            <button
              onClick={onClose}
              className="text-xs px-3 py-1.5 rounded-lg cursor-pointer"
              style={{ color: C.textMuted, border: `1px solid ${C.borderSubtle}`, background: C.borderSubtle }}
            >
              {t("onboardClose")}
            </button>
            <button
              onClick={start}
              disabled={!canStart || busy || running}
              data-testid="onboard-start"
              className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
              style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.accent }}
            >
              {(busy || running) && <Loader2 size={11} className="animate-spin" />}
              {running ? t("onboardRunning") : t("onboardStart")}
            </button>
          </>
        )}
      </div>
    </ResponsiveModal>
  );
}

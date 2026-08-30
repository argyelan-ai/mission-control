"use client";

/**
 * NodePairingDialog — "Gerät meldet sich selbst" (Fleet & Rezepte v2, Phase 1/2).
 *
 * Alternative zum SSH-Auto-Onboarding (HostOnboardDialog) für Boxen, die MC
 * nicht per SSH erreicht: ein Pairing-Code + fertiger Install-Einzeiler
 * (POST /api/v1/nodes/pairing-codes), den der Operator auf dem Gerät
 * ausführt — es meldet sich danach selbst per Push-Telemetrie
 * (routers/nodes.py, kind='agent').
 *
 * Modal-Chrome ist ResponsiveModal (review finding #10, 30.08.2026 — 9
 * andere Dialoge nutzen es bereits), nicht handgebaut.
 */

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Check, Copy, Loader2, X } from "lucide-react";
import { api } from "@/lib/api";
import type { NodePairingCodeResponse } from "@/lib/types";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { C, STATUS_TEXT } from "@/lib/colors";
import { extractApiError } from "@/components/shared/SshProcessDeployDialog";

export function NodePairingDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useTranslations("runtimes.hosts");

  const [displayNameHint, setDisplayNameHint] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<NodePairingCodeResponse | null>(null);
  const [copied, setCopied] = useState(false);

  // ResponsiveModal keeps this mounted across opens — reset explicitly
  // instead of relying on the parent remounting it (review finding #10).
  useEffect(() => {
    if (!open) return;
    setDisplayNameHint(""); setBusy(false); setError(null); setResult(null); setCopied(false);
  }, [open]);

  async function generate() {
    setBusy(true);
    setError(null);
    try {
      const res = await api.nodes.createPairingCode({
        display_name_hint: displayNameHint.trim() || null,
      });
      setResult(res);
    } catch (err) {
      setError(extractApiError(err));
    } finally {
      setBusy(false);
    }
  }

  async function copyInstallCommand() {
    if (!result) return;
    try {
      await navigator.clipboard.writeText(result.install_command);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard unavailable — the command is still selectable in the box below */
    }
  }

  return (
    <ResponsiveModal open={open} onClose={onClose} aria-labelledby="node-pairing-title">
      <div
        className="flex items-center justify-between p-5 border-b shrink-0"
        style={{ borderColor: "var(--color-border)" }}
      >
        <h2 id="node-pairing-title" className="text-sm font-semibold" style={{ color: C.textPrimary }}>
          {t("pairingDialogTitle")}
        </h2>
        <button
          onClick={onClose}
          aria-label={t("pairingClose")}
          className="p-1 rounded-md hover:bg-[var(--color-bg-hover)] cursor-pointer"
        >
          <X size={14} style={{ color: C.textMuted }} />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-5 flex flex-col gap-3">
        <p className="text-[11px]" style={{ color: C.textMuted }}>{t("pairingIntro")}</p>

        {!result && (
          <label className="flex flex-col gap-1">
            <span className="text-xs" style={{ color: C.textMuted }}>{t("pairingFieldDisplayName")}</span>
            <input
              type="text"
              value={displayNameHint}
              onChange={(e) => setDisplayNameHint(e.target.value)}
              disabled={busy}
              data-testid="pairing-display-name"
              className="text-sm px-3 py-2 rounded-lg outline-none disabled:opacity-50"
              style={{ background: C.border, border: `1px solid ${C.borderSubtle}`, color: C.textPrimary }}
            />
          </label>
        )}

        {error && (
          <div
            className="text-xs px-3 py-2 rounded-lg"
            style={{ background: `${C.error}14`, border: `1px solid ${C.error}33`, color: STATUS_TEXT.error }}
          >
            {error}
          </div>
        )}

        {result && (
          <div className="flex flex-col gap-3">
            <div className="flex flex-col gap-1">
              <span className="text-xs" style={{ color: C.textMuted }}>{t("pairingCodeLabel")}</span>
              <div
                data-testid="pairing-code"
                className="text-lg font-mono font-semibold tracking-widest px-3 py-2 rounded-lg text-center"
                style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.accent }}
              >
                {result.code}
              </div>
              <span className="text-[10px]" style={{ color: C.textMuted }}>
                {t("pairingExpiresLabel")}: {new Date(result.expires_at).toLocaleString()}
              </span>
            </div>

            <div className="flex flex-col gap-1">
              <span className="text-xs" style={{ color: C.textMuted }}>{t("pairingInstallCommandLabel")}</span>
              <div className="flex items-start gap-2">
                <code
                  data-testid="pairing-install-command"
                  className="flex-1 text-[10px] px-2.5 py-2 rounded-lg font-mono break-all"
                  style={{ background: C.border, border: `1px solid ${C.borderSubtle}`, color: C.textPrimary }}
                >
                  {result.install_command}
                </code>
                <button
                  onClick={copyInstallCommand}
                  data-testid="pairing-copy"
                  className="flex items-center gap-1 text-xs px-2.5 py-2 rounded-lg cursor-pointer shrink-0"
                  style={{ background: C.borderSubtle, border: `1px solid ${C.border}`, color: C.textMuted }}
                >
                  {copied ? <Check size={12} /> : <Copy size={12} />}
                  {copied ? t("pairingCopied") : t("pairingCopy")}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>

      <div
        className="flex items-center justify-end gap-2 px-5 py-3 border-t shrink-0"
        style={{ borderColor: "var(--color-border)", paddingBottom: "calc(env(safe-area-inset-bottom) + 0.75rem)" }}
      >
        <button
          onClick={onClose}
          className="text-xs px-3 py-1.5 rounded-lg cursor-pointer"
          style={{ color: C.textMuted, border: `1px solid ${C.borderSubtle}`, background: C.borderSubtle }}
        >
          {t("pairingClose")}
        </button>
        {!result && (
          <button
            onClick={generate}
            disabled={busy}
            data-testid="pairing-generate"
            className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
            style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.accent }}
          >
            {busy && <Loader2 size={11} className="animate-spin" />}
            {t("pairingGenerate")}
          </button>
        )}
      </div>
    </ResponsiveModal>
  );
}

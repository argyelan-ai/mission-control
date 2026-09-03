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
 * Rezept-Umschalter P2 (02.09.2026):
 *  - Rolle (Head/Worker): Vorschlag aus dem Bestand wie im Wizard (erste Box
 *    Head, weitere Worker), Handklick gewinnt; geht als `role` mit.
 *  - Optionales Feld „SSH-Adresse": wird ans Pairing mitgegeben, damit der
 *    kind=agent-Host ZUSÄTZLICH per SSH startbar ist. Ohne sie meldet die
 *    Box nur — starten kann MC sie dann nicht (der Satz steht am Feld).
 *  - Mehrere Install-Befehle: das Backend baut je erreichbarer Adresse des
 *    MC-Hosts einen Einzeiler (`install_commands`, z. B. Tailscale + LAN).
 *    Die UI zeigt alle untereinander mit Beschriftung und eigenem Kopier-
 *    knopf. Fehlt die Liste (altes Backend), bleibt `install_command` als
 *    einzige Zeile — kein Bruch beim gemischten Deploy.
 *
 * Modal-Chrome ist ResponsiveModal (review finding #10, 30.08.2026 — 9
 * andere Dialoge nutzen es bereits), nicht handgebaut.
 */

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Check, Copy, Loader2, X } from "lucide-react";
import { api } from "@/lib/api";
import type { HostRole, NodeInstallCommand, NodePairingCodeResponse } from "@/lib/types";
import { RoleField, suggestRole } from "./RoleField";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { C, STATUS_TEXT } from "@/lib/colors";
import { extractApiError } from "@/components/shared/SshProcessDeployDialog";

/**
 * Liste der Install-Zeilen — neu aus `install_commands`, alt aus
 * `install_command`. Reine Funktion, separat testbar (Fallback-Probe).
 */
export function installCommandRows(res: NodePairingCodeResponse): NodeInstallCommand[] {
  if (res.install_commands && res.install_commands.length > 0) return res.install_commands;
  return [{ label: "", url: "", cmd: res.install_command }];
}

/**
 * Backend-Label → Satz in der Oberfläche; Unbekanntes bleibt, wie es kommt.
 * Das Backend liefert (Stand 02.09.): "Tailscale", "LAN", "Adresse" (DNS-Name),
 * "Öffentlich"; Dubletten als "LAN 2" — deshalb Präfix-Treffer plus Suffix.
 */
const INSTALL_LABEL_KEY: Record<string, string> = {
  tailscale: "pairingInstallLabelTailscale",
  tailnet: "pairingInstallLabelTailscale",
  lan: "pairingInstallLabelLan",
  adresse: "pairingInstallLabelAddress",
  address: "pairingInstallLabelAddress",
  "öffentlich": "pairingInstallLabelPublic",
  public: "pairingInstallLabelPublic",
};

/** Übersetzt "LAN 2" zu „im LAN 2": Wort übersetzen, Zähler anhängen. */
export function installLabelParts(raw: string): { key: string | null; suffix: string } {
  const m = raw.trim().toLowerCase().match(/^(.*?)(?:\s+(\d+))?$/);
  const word = m?.[1] ?? "";
  return { key: INSTALL_LABEL_KEY[word] ?? null, suffix: m?.[2] ? ` ${m[2]}` : "" };
}

export function NodePairingDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useTranslations("runtimes.hosts");

  const [displayNameHint, setDisplayNameHint] = useState("");
  const [sshHost, setSshHost] = useState("");
  const [role, setRole] = useState<HostRole | null>(null);
  /** true, solange niemand geklickt hat — dann darf der Bestand vorbelegen. */
  const [roleSuggested, setRoleSuggested] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<NodePairingCodeResponse | null>(null);
  /** Index der zuletzt kopierten Zeile — je Zeile ein eigener „Kopiert!". */
  const [copiedIdx, setCopiedIdx] = useState<number | null>(null);

  // ResponsiveModal keeps this mounted across opens — reset explicitly
  // instead of relying on the parent remounting it (review finding #10).
  useEffect(() => {
    if (!open) return;
    setDisplayNameHint(""); setSshHost(""); setBusy(false); setError(null); setResult(null); setCopiedIdx(null);
    setRole(null); setRoleSuggested(true);
    // Rollen-Vorschlag aus dem Bestand (P2). Kein useQuery: der Dialog läuft
    // auch ohne QueryClientProvider (Tests, Einzelverwendung). Schlägt die
    // Liste fehl oder ist sie noch nicht da, bleibt die Rolle null — ein
    // stiller „Head"-Vorschlag ohne Wissen wäre eine Falschaussage.
    let cancelled = false;
    api.hosts.list()
      .then((hosts) => { if (!cancelled) setRole((r) => r ?? suggestRole(hosts.length)); })
      .catch(() => { /* Bestand unbekannt → kein Vorschlag, role bleibt null */ });
    return () => { cancelled = true; };
  }, [open]);

  async function generate() {
    setBusy(true);
    setError(null);
    try {
      const res = await api.nodes.createPairingCode({
        display_name_hint: displayNameHint.trim() || null,
        ssh_host: sshHost.trim() || null,
        // null = Bestand unbekannt oder bewusst nichts gewählt — nie ein stilles „head".
        role,
      });
      setResult(res);
    } catch (err) {
      setError(extractApiError(err));
    } finally {
      setBusy(false);
    }
  }

  async function copyRow(idx: number, cmd: string) {
    try {
      await navigator.clipboard.writeText(cmd);
      setCopiedIdx(idx);
      setTimeout(() => setCopiedIdx((cur) => (cur === idx ? null : cur)), 2000);
    } catch {
      /* clipboard unavailable — the command is still selectable in the box below */
    }
  }

  const labelFor = (row: NodeInstallCommand): string => {
    const { key, suffix } = installLabelParts(row.label);
    return key ? `${t(key)}${suffix}` : row.label;
  };

  const rows = result ? installCommandRows(result) : [];

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
          <>
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

            <RoleField
              idPrefix="pairing-role"
              value={role}
              onChange={(r) => { setRole(r); setRoleSuggested(false); }}
              labelClassName="text-xs text-[var(--color-text-muted)]"
              suggested={roleSuggested}
            />

            {/* P2: SSH-Adresse — optional, aber der Unterschied zwischen
                „MC sieht die Box" und „MC kann sie starten". */}
            <label className="flex flex-col gap-1">
              <span className="text-xs" style={{ color: C.textMuted }}>{t("pairingFieldSshHost")}</span>
              <input
                type="text"
                value={sshHost}
                onChange={(e) => setSshHost(e.target.value)}
                disabled={busy}
                placeholder={t("sshHostPlaceholder")}
                aria-describedby="pairing-ssh-hint"
                data-testid="pairing-ssh-host"
                className="text-sm px-3 py-2 rounded-lg outline-none font-mono disabled:opacity-50"
                style={{ background: C.border, border: `1px solid ${C.borderSubtle}`, color: C.textPrimary }}
              />
              <span id="pairing-ssh-hint" className="text-[11px] leading-relaxed" style={{ color: C.textMuted }}>
                {t("pairingSshHint")}
              </span>
            </label>
          </>
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

            <div className="flex flex-col gap-1.5">
              <span className="text-xs" style={{ color: C.textMuted }}>
                {rows.length > 1 ? t("pairingInstallCommandsLabel") : t("pairingInstallCommandLabel")}
              </span>
              <ul className="flex flex-col gap-2 list-none m-0 p-0" data-testid="pairing-install-commands">
                {rows.map((row, idx) => {
                  const label = labelFor(row);
                  const copied = copiedIdx === idx;
                  return (
                    <li key={`${row.label}-${idx}`} className="flex flex-col gap-1">
                      {label && (
                        <span
                          data-testid={`pairing-install-label-${idx}`}
                          className="text-[10px] font-mono uppercase tracking-wider"
                          style={{ color: C.textSecondary }}
                        >
                          {label}
                        </span>
                      )}
                      <div className="flex items-start gap-2">
                        <code
                          data-testid={idx === 0 ? "pairing-install-command" : `pairing-install-command-${idx}`}
                          className="flex-1 text-[10px] px-2.5 py-2 rounded-lg font-mono break-all"
                          style={{ background: C.border, border: `1px solid ${C.borderSubtle}`, color: C.textPrimary }}
                        >
                          {row.cmd}
                        </code>
                        <button
                          onClick={() => copyRow(idx, row.cmd)}
                          data-testid={idx === 0 ? "pairing-copy" : `pairing-copy-${idx}`}
                          aria-label={label ? t("pairingCopyAria", { label }) : t("pairingCopy")}
                          className="flex items-center gap-1 text-xs px-2.5 py-2 min-h-11 sm:min-h-0 rounded-lg cursor-pointer shrink-0 transition-colors"
                          style={{
                            background: copied ? C.accentSubtle : C.borderSubtle,
                            border: `1px solid ${copied ? C.borderAccent : C.border}`,
                            color: copied ? C.accent : C.textMuted,
                          }}
                        >
                          {copied ? <Check size={12} /> : <Copy size={12} />}
                          {copied ? t("pairingCopied") : t("pairingCopy")}
                        </button>
                      </div>
                    </li>
                  );
                })}
              </ul>
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

"use client";

import { useState, Fragment } from "react";
import { useTranslations } from "next-intl";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { RotateCcw } from "lucide-react";
import { cn } from "@/lib/utils";
import { C } from "@/lib/colors";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { RUNTIME_TYPE_COLOR } from "@/components/shared/RuntimePill";
import { RuntimeSwitchModal } from "@/components/shared/RuntimeSwitchModal";
import type { Agent } from "@/lib/types";
import {
  groupRuntimesByProvider,
  isRuntimeBlockedByLocality,
  splitSlotRuntimes,
  allowsRuntimeFallback,
} from "@/lib/groupRuntimes";

// ── Runtime Selection Section ─────────────────────────────────────────────
// cli-bridge agents switch runtimes the "normal" way (container restart).
// Host agents with a HostHarnessAdapter (ADR-060/ADR-064) switch in place —
// same PATCH /agents/{id} endpoint, backend routes it to the in-place path.
// Host agents WITHOUT an adapter still show a locked badge — managed via
// launchd on the host, no MC-side runtime concept.
// Phase 30 dropped the `openclaw` runtime entirely (CHECK constraint on
// agents.agent_runtime). Color map reused from RuntimePill (defined above).

// Own module so the test can mount it alone, and because a Next.js page
// file may only export its default component.
export function RuntimeSelectionSection({ agent, agentId }: { agent: Agent; agentId: string }) {
  const t = useTranslations("agents.detail");
  const tSlot = useTranslations("runtimes.slot");
  const qc = useQueryClient();
  // Backend-derived (Agent.runtime_switchable). Never re-derive from harness:
  // the old `harness === "hermes"` compare locked grok/kimi/claude host agents
  // out of the picker for weeks after the backend learned to switch them.
  const isSwitchable = agent.runtime_switchable;
  // Host-inplace only steers UI details (no harness selector, in-place copy) —
  // derived from the backend verdict, not from a harness allowlist.
  const isHostInplace = agent.agent_runtime === "host" && isSwitchable;

  const { data: runtimesData, isError: runtimesError } = useQuery({
    queryKey: ["runtimes"],
    queryFn: () => api.runtimes.list(),
    enabled: isSwitchable,
  });

  const [selected, setSelected] = useState<string | null>(agent.runtime_id ?? null);
  const [modalOpen, setModalOpen] = useState(false);
  const dirty = selected !== (agent.runtime_id ?? null);

  // Slot-Runtime (ADR-078): die festen Box-Adressen stehen oben in einer
  // eigenen Gruppe, alles andere behält die Reihenfolge des Servers.
  const { slots: slotRuntimes, rest: otherRuntimes } = splitSlotRuntimes(
    runtimesData?.runtimes ?? []
  );

  const selectedRuntime = runtimesData?.runtimes.find((r) => r.id === selected || r.slug === selected);
  // D-2: the bound runtime must always be a visible, selected option. While
  // the list loads (or when the bound row is missing from it — disabled,
  // removed) a <select> without a matching <option> silently shows the first
  // entry ("Fallback") or nothing, contradicting the header.
  const boundId = agent.runtime_id ?? null;
  const boundInList =
    !!boundId && !!runtimesData?.runtimes.some((r) => r.id === boundId || r.slug === boundId);
  const showBoundPlaceholder = !!boundId && !boundInList;
  const borderColor = isSwitchable && selectedRuntime
    ? RUNTIME_TYPE_COLOR[selectedRuntime.runtime_type] ?? "var(--color-border)"
    : "var(--color-border)";

  if (!isSwitchable) {
    // Locked badge for agents the backend refuses to switch. The text is the
    // backend's own reason (host_harness_adapter.runtime_switch_availability),
    // never a hardcoded sentence — the previous literal named a model
    // ("Boss = Opus 4.7") that had long since rotted.
    const reason =
      agent.runtime_switch_blocked_reason ?? t("runtimeSwitchUnsupported");
    return (
      <div
        className="rounded-xl p-4"
        style={{
          backgroundColor: "var(--color-bg-surface)",
          border: "1px solid var(--color-border)",
        }}
      >
        <div className="flex items-center gap-2 mb-2">
          <span className="text-xs font-mono text-[var(--color-text-muted)]">{t("runtimeLabel")}</span>
          <span
            className="text-[9px] px-1.5 py-0.5 rounded-sm font-mono uppercase tracking-wide"
            style={{
              backgroundColor: "var(--color-bg-elevated)",
              color: C.textSecondary,
              border: "1px solid var(--color-border)",
            }}
          >
            locked · {agent.agent_runtime}
          </span>
        </div>
        <div className="text-[11px] text-[var(--color-text-muted)]">{reason}</div>
      </div>
    );
  }

  return (
    <>
      <div
        className="rounded-xl p-4"
        style={{
          backgroundColor: "var(--color-bg-surface)",
          border: `1px solid ${borderColor}`,
          borderLeft: `3px solid ${borderColor}`,
        }}
      >
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-2">
              <span className="text-xs font-mono text-[var(--color-text-muted)]">{t("runtimeLabel")}</span>
              {selectedRuntime?.state === "ready" && (
                <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: C.online }} />
              )}
              {selectedRuntime?.state && selectedRuntime.state !== "ready" && (
                <span className="text-[9px] font-mono uppercase text-[var(--color-text-muted)]">
                  {selectedRuntime.state}
                </span>
              )}
            </div>
            <select
              value={selected ?? ""}
              onChange={(e) => setSelected(e.target.value === "" ? null : e.target.value)}
              className="w-full text-sm rounded-lg px-3 py-2 outline-none cursor-pointer"
              style={{
                backgroundColor: "var(--color-bg-deep)",
                border: `1px solid ${dirty ? C.borderAccent : "var(--color-border)"}`,
                color: "var(--color-text-primary)",
              }}
            >
              {/* Slot-Runtime (ADR-078): omp braucht eine gebundene Runtime.
                  Ohne sie startet der Harness ohne Modell, und das Backend
                  lehnt das Lösen der Bindung ohnehin mit 422 ab — die Option
                  gar nicht anzubieten ist ehrlicher als ein Klick ins Leere. */}
              {showBoundPlaceholder && (
                <option value={boundId!}>
                  {runtimesData
                    ? t("currentBindingMissing")
                    : runtimesError
                      ? t("currentBindingLoadFailed")
                      : t("currentBindingLoading")}
                </option>
              )}
              {allowsRuntimeFallback(agent.harness) && (
                <option value="">{t("fallbackOption")}</option>
              )}
              {/* Die festen Box-Adressen zuerst: an ihnen hängen die Agenten,
                  sie folgen dem Modell, das gerade auf der Box läuft. Der Name
                  kommt FERTIG vom Server („BOX-A :8000 (aktuell: <Modell>)") —
                  hier wird nichts angehängt, sonst stünde das Modell zweimal. */}
              {slotRuntimes.length > 0 && (
                <optgroup label={tSlot("pickerGroup")}>
                  {slotRuntimes.map((r) => (
                    <option key={r.id} value={r.id} disabled={!r.enabled}>
                      {r.display_name}
                      {!r.enabled ? ` · ${t("runtimeDisabled")}` : ""}
                    </option>
                  ))}
                </optgroup>
              )}
              {/* Grouped by vendor via <optgroup>: the API already returns the
                  rows in provider order, this only makes that visible. The
                  label comes from the server (`provider_label`) — deriving it
                  here would be a second copy of a backend rule. Rows without a
                  recognised vendor (local vLLM, LM Studio) keep their flat
                  position after the grouped ones.

                  Phase 0 (Verbund-UI, 30.08.2026): a host-inplace agent can
                  only ever run something physically on ITS OWN box — a cloud
                  runtime (Anthropic subscription, Ollama Cloud, …) is never a
                  real candidate there. Disabled-with-reason, not filtered out
                  entirely, matching the existing !r.enabled pattern below —
                  the row stays visible so the "why not" is explained instead
                  of the option just silently disappearing. */}
              {groupRuntimesByProvider(otherRuntimes).map(
                ({ label, runtimes }) => {
                  const options = runtimes.map((r) => {
                    const cloudBlocked = isRuntimeBlockedByLocality(r, isHostInplace);
                    const disabled = !r.enabled || cloudBlocked;
                    return (
                      <option key={r.id} value={r.id} disabled={disabled}>
                        {r.display_name} · {r.runtime_type}
                        {r.model_identifier ? ` · ${r.model_identifier}` : ""}
                        {!r.enabled
                          ? ` · ${t("runtimeDisabled")}`
                          : cloudBlocked
                            ? ` · ${t("runtimeCloudUnavailable")}`
                            : ""}
                      </option>
                    );
                  });
                  return label ? (
                    <optgroup key={label} label={label}>
                      {options}
                    </optgroup>
                  ) : (
                    <Fragment key="__ungrouped">{options}</Fragment>
                  );
                },
              )}
            </select>
            <div className="text-[10px] text-[var(--color-text-muted)] mt-1.5">
              {isHostInplace ? (
                <>{t("inplaceHint")}</>
              ) : (
                <>
                  {t("dockerHintBefore")} <code className="font-mono">docker restart</code>{" "}
                  {t("dockerHintAfter")}
                </>
              )}
            </div>
          </div>
          <div className="pt-[22px]">
            <button
              onClick={() => {
                if (!dirty) return;
                setModalOpen(true);
              }}
              disabled={!dirty}
              className={cn(
                "flex items-center gap-1.5 text-xs px-3 py-2 rounded-lg whitespace-nowrap transition-all",
                !dirty ? "cursor-not-allowed opacity-40" : "cursor-pointer",
              )}
              style={{ backgroundColor: C.accent, color: C.onAccent }}
            >
              <RotateCcw size={12} />
              {t("switchButton")}
            </button>
          </div>
        </div>
      </div>

      {/* Phase 15 T3.1 — confirm modal with dry-run preview + force toggle */}
      <RuntimeSwitchModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        agent={agent}
        targetRuntimeId={selected}
        onConfirm={async ({ force_when_in_progress, harness }) => {
          const res = await api.agents.switchRuntime(agentId, selected, {
            force_when_in_progress,
            harness,
          });
          qc.invalidateQueries({ queryKey: ["agent", agentId] });
          qc.invalidateQueries({ queryKey: ["agents"] });
          qc.invalidateQueries({ queryKey: ["runtimes"] });
          qc.invalidateQueries({ queryKey: ["runtime-switch-preview", agentId] });
          // Task #26 — the switch now auto-triggers the agent restart; make
          // sure "switched" is never mistaken for "already running the new
          // model" when that restart was skipped or failed.
          if (res._switch?.restart_failed) {
            notify.error(t("switchedRestartFailed"));
          } else if (res._switch?.restart_skipped) {
            notify.success(t("switchedRestartPending"));
          } else {
            notify.success(
              res._switch?.image_switched
                ? t("switchedRebuilt", { s: Math.round((res._switch?.duration_ms ?? 0) / 1000) })
                : t("switched"),
            );
          }
          return res._switch ?? null;
        }}
      />
    </>
  );
}

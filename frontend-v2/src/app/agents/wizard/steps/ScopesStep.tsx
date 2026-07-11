"use client";

import { useEffect, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { RotateCcw } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import type { WizardStepProps } from "../types";
import { wizardLabelClass } from "../shared";
import { ALL_SCOPES, defaultScopesForRole } from "../scopeDefaults";

export function ScopesStep({ state, update }: WizardStepProps) {
  const roleDefaults = useMemo(
    () => defaultScopesForRole(state.role, state.isBoardLead),
    [state.role, state.isBoardLead]
  );

  // Prefill from role defaults the first time we land here with no scopes —
  // never leave scopes empty (empty = ALL scopes on the backend).
  useEffect(() => {
    if (state.scopes.length === 0) update({ scopes: roleDefaults });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const { data: pluginsData } = useQuery({
    queryKey: ["plugins"],
    queryFn: () => api.plugins.list(),
  });
  const plugins = pluginsData?.plugins ?? [];

  function toggle(scope: string) {
    const has = state.scopes.includes(scope);
    update({
      scopes: has ? state.scopes.filter((s) => s !== scope) : [...state.scopes, scope],
    });
  }

  function togglePlugin(key: string) {
    const cur = state.cliPlugins ?? [];
    const has = cur.includes(key);
    update({ cliPlugins: has ? cur.filter((p) => p !== key) : [...cur, key] });
  }

  return (
    <div className="space-y-5">
      <div>
        <div className="flex items-center justify-between mb-2">
          <label className={`${wizardLabelClass} mb-0`}>Scopes ({state.scopes.length})</label>
          <button
            onClick={() => update({ scopes: roleDefaults })}
            className="flex items-center gap-1 text-[11px] cursor-pointer text-[var(--color-text-muted)] hover:text-[var(--color-text-secondary)]"
          >
            <RotateCcw size={11} /> Rollen-Default
          </button>
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-1.5">
          {ALL_SCOPES.map((scope) => {
            const checked = state.scopes.includes(scope);
            const isDefault = roleDefaults.includes(scope);
            const deviates = checked !== isDefault;
            return (
              <label
                key={scope}
                className="flex items-center gap-2 rounded-lg px-2.5 py-2 cursor-pointer transition-colors"
                style={{
                  backgroundColor: checked ? C.accentSubtle : "rgba(255,255,255,0.03)",
                  border: `1px solid ${deviates ? C.warning + "66" : checked ? C.borderAccent : C.borderSubtle}`,
                }}
                title={deviates ? "Weicht vom Rollen-Default ab" : undefined}
              >
                <input
                  type="checkbox"
                  aria-label={scope}
                  checked={checked}
                  onChange={() => toggle(scope)}
                  className="cursor-pointer accent-[var(--color-accent)]"
                />
                <span className="text-[11px] font-mono text-[var(--color-text-secondary)] truncate">
                  {scope}
                </span>
              </label>
            );
          })}
        </div>
        <p className="mt-2 text-[10px] text-[var(--color-text-muted)]">
          Orange umrandet = weicht vom Rollen-Default ab. Ein Agent muss mindestens
          einen Scope haben — leere Liste würde serverseitig ALLE Rechte bedeuten.
        </p>
      </div>

      {plugins.length > 0 && (
        <div>
          <label className={wizardLabelClass}>CLI Plugins (optional)</label>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5 max-h-40 overflow-y-auto">
            {plugins.map((p) => {
              const checked = (state.cliPlugins ?? []).includes(p.key);
              return (
                <label
                  key={p.key}
                  className="flex items-center gap-2 rounded-lg px-2.5 py-2 cursor-pointer"
                  style={{
                    backgroundColor: checked ? C.accentSubtle : "rgba(255,255,255,0.03)",
                    border: `1px solid ${checked ? C.borderAccent : C.borderSubtle}`,
                  }}
                >
                  <input
                    type="checkbox"
                    aria-label={p.key}
                    checked={checked}
                    onChange={() => togglePlugin(p.key)}
                    className="cursor-pointer accent-[var(--color-accent)]"
                  />
                  <span className="text-[11px] font-mono text-[var(--color-text-secondary)] truncate">
                    {p.name}
                  </span>
                </label>
              );
            })}
          </div>
          <p className="mt-1.5 text-[10px] text-[var(--color-text-muted)]">
            Leer = alle installierten Plugins (Standard).
          </p>
        </div>
      )}
    </div>
  );
}

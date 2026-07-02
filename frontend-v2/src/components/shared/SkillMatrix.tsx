"use client";
import { useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Save, Undo2, Loader2, Check, Zap } from "lucide-react";
import { api } from "@/lib/api";
import type { Agent } from "@/lib/types";
import { notify } from "@/lib/notify";
import { C } from "@/lib/colors";

/**
 * SkillMatrix — Custom Skills pro Agent ein-/ausschalten.
 *
 * Analog zur PluginMatrix, aber fuer ~/.openclaw/skills/ Custom Skills.
 * Steuert agents.cli_skills (null=alle, []=keine, ["x"]=Allowlist).
 */

interface SkillAssignment {
  [agentId: string]: Set<string>;
}

export function SkillMatrix() {
  const qc = useQueryClient();
  const [dirty, setDirty] = useState<SkillAssignment>({});
  const [saving, setSaving] = useState(false);

  const { data: skillsData, isLoading: skillsLoading } = useQuery({
    queryKey: ["custom-skills"],
    queryFn: () => api.plugins.listCustomSkills(),
    staleTime: 60_000,
  });

  const { data: agentsData, isLoading: agentsLoading } = useQuery({
    queryKey: ["agents-all"],
    queryFn: () => api.agents.list(undefined, true),
    staleTime: 60_000,
  });

  const skills = skillsData?.skills ?? [];
  const agents: Agent[] = (agentsData ?? []).filter(
    (a: Agent) => a.agent_runtime === "cli-bridge"
  );

  // Initiale Zuweisung aus DB: cli_skills null = alle, [] = keine, [...] = nur diese
  const initial = useMemo(() => {
    const map: SkillAssignment = {};
    for (const agent of agents) {
      if (agent.cli_skills === null || agent.cli_skills === undefined) {
        // null = alle Skills
        map[agent.id] = new Set(skills.map((s) => s.name));
      } else {
        map[agent.id] = new Set(agent.cli_skills as string[]);
      }
    }
    return map;
  }, [agents, skills]);

  // Merged state: initial + dirty overrides
  function getState(agentId: string): Set<string> {
    return dirty[agentId] ?? initial[agentId] ?? new Set();
  }

  function toggle(agentId: string, skillName: string) {
    const current = new Set(getState(agentId));
    if (current.has(skillName)) current.delete(skillName);
    else current.add(skillName);
    setDirty((prev) => ({ ...prev, [agentId]: current }));
  }

  function toggleAll(agentId: string) {
    const current = getState(agentId);
    const allEnabled = skills.every((s) => current.has(s.name));
    const next = allEnabled ? new Set<string>() : new Set(skills.map((s) => s.name));
    setDirty((prev) => ({ ...prev, [agentId]: next }));
  }

  const hasDirty = Object.keys(dirty).length > 0;

  const saveMutation = useMutation({
    mutationFn: async () => {
      const promises = Object.entries(dirty).map(([agentId, enabledSkills]) => {
        const allEnabled = skills.length > 0 && enabledSkills.size === skills.length;
        return api.skills.setAgentSkills(agentId, {
          cli_skills: allEnabled ? null : Array.from(enabledSkills),
          update_cli_skills: true,
        });
      });
      return Promise.all(promises);
    },
    onSuccess: () => {
      setDirty({});
      qc.invalidateQueries({ queryKey: ["agents-all"] });
      notify.success("Skills gespeichert");
    },
    onError: () => notify.error("Fehler beim Speichern"),
  });

  function handleSave() {
    setSaving(true);
    saveMutation.mutate(undefined, { onSettled: () => setSaving(false) });
  }

  if (skillsLoading || agentsLoading) {
    return (
      <div className="flex items-center gap-2 text-xs py-8" style={{ color: "var(--color-text-muted)" }}>
        <Loader2 size={12} className="animate-spin" /> Lade Skills + Agents...
      </div>
    );
  }

  if (skills.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-12 gap-2">
        <Zap size={24} style={{ color: "var(--color-text-muted)" }} />
        <p className="text-sm" style={{ color: "var(--color-text-muted)" }}>Keine Custom Skills gefunden.</p>
        <p className="text-xs" style={{ color: "var(--color-text-muted)" }}>
          Skills leben in <code style={{ color: C.info }}>~/.openclaw/skills/</code>
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Zap size={14} style={{ color: C.accent }} />
          <span className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
            Custom Skills ({skills.length})
          </span>
          <span className="text-xs" style={{ color: "var(--color-text-muted)" }}>
            ~/.openclaw/skills/
          </span>
        </div>

        {hasDirty && (
          <div className="flex items-center gap-2">
            <button
              onClick={() => setDirty({})}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs cursor-pointer"
              style={{ color: "var(--color-text-muted)" }}
            >
              <Undo2 size={11} /> Zuruecksetzen
            </button>
            <button
              onClick={handleSave}
              disabled={saving}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium cursor-pointer"
              style={{
                background: `${C.accent}26`,
                border: `1px solid ${C.accent}4D`,
                color: C.accentHover,
              }}
            >
              {saving ? <Loader2 size={11} className="animate-spin" /> : <Save size={11} />}
              Speichern
            </button>
          </div>
        )}
      </div>

      {/* Matrix — Mobile: horizontal scroll with sticky first column (M11/M17) */}
      <div
        className="rounded-xl overflow-x-auto"
        style={{ border: "1px solid rgba(255,255,255,0.06)", overscrollBehaviorX: "contain" } as React.CSSProperties}
        tabIndex={0}
        role="region"
        aria-label="Skill Team-Zuweisungen"
      >
        <table className="w-full text-xs">
          <thead>
            <tr style={{ background: "rgba(255,255,255,0.02)" }}>
              {/* Sticky "Skill" header — opaque bg prevents row content bleed-through */}
              <th
                className="sticky left-0 z-10 text-left px-3 py-2.5 font-semibold"
                style={{ color: "var(--color-text-muted)", minWidth: 180, backgroundColor: C.bgElevated }}
              >
                Skill
              </th>
              {agents.map((agent) => {
                const state = getState(agent.id);
                const allEnabled = skills.every((s) => state.has(s.name));
                return (
                  <th key={agent.id} className="px-2 py-2.5 text-center font-medium" style={{ color: "var(--color-text-secondary)", minWidth: 80 }}>
                    <button
                      onClick={() => toggleAll(agent.id)}
                      className="cursor-pointer hover:underline"
                      title={allEnabled ? "Alle deaktivieren" : "Alle aktivieren"}
                    >
                      {agent.name}
                    </button>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {skills.map((skill, i) => (
              <tr
                key={skill.name}
                style={{
                  borderTop: i > 0 ? "1px solid rgba(255,255,255,0.04)" : "none",
                  background: i % 2 === 0 ? "transparent" : "rgba(255,255,255,0.01)",
                }}
              >
                {/* Sticky Skill name cell — opaque bg required (M11: no alpha, content must not bleed through) */}
                <td className="sticky left-0 z-10 px-3 py-2" style={{ color: "var(--color-text-primary)", backgroundColor: C.bgElevated }}>
                  <div className="font-medium">{skill.name}</div>
                  {skill.description && (
                    <div className="text-[10px] mt-0.5 truncate max-w-[200px]" style={{ color: "var(--color-text-muted)" }}>
                      {skill.description}
                    </div>
                  )}
                </td>
                {agents.map((agent) => {
                  const enabled = getState(agent.id).has(skill.name);
                  const wasChanged = dirty[agent.id] !== undefined;
                  const wasEnabled = (initial[agent.id] ?? new Set()).has(skill.name);
                  const changed = wasChanged && enabled !== wasEnabled;

                  return (
                    <td key={agent.id} className="px-2 py-2 text-center">
                      <button
                        onClick={() => toggle(agent.id, skill.name)}
                        className="w-5 h-5 rounded flex items-center justify-center cursor-pointer transition-colors mx-auto"
                        style={{
                          background: enabled ? `${C.accent}33` : "rgba(255,255,255,0.04)",
                          border: changed
                            ? `2px solid ${C.warning}`
                            : enabled
                              ? `1px solid ${C.accent}66`
                              : "1px solid rgba(255,255,255,0.08)",
                        }}
                      >
                        {enabled && <Check size={11} style={{ color: C.accentHover }} />}
                      </button>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

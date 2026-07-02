"use client";
import { useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Save, Undo2, Loader2, Check, Github, ChevronDown, ChevronRight } from "lucide-react";
import { api } from "@/lib/api";
import type { Agent, CliPlugin, GithubSkillRepo } from "@/lib/types";
import { notify } from "@/lib/notify";
import { C } from "@/lib/colors";

interface PluginAssignment {
  [agentId: string]: Set<string>;
}

function GithubSkillsSection() {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const { data, isLoading } = useQuery({
    queryKey: ["github-skills"],
    queryFn: () => api.plugins.listGithubSkills(),
    staleTime: 60_000,
  });

  const repos: GithubSkillRepo[] = data?.repos ?? [];

  if (isLoading) return (
    <div className="flex items-center gap-2 text-xs py-4" style={{ color: "var(--color-text-muted)" }}>
      <Loader2 size={12} className="animate-spin" /> Lade GitHub Skills...
    </div>
  );

  if (!repos.length) return (
    <div className="text-xs py-4" style={{ color: "var(--color-text-muted)" }}>
      Keine GitHub Skill-Repos installiert.{" "}
      <span style={{ color: "rgba(255,255,255,0.3)" }}>
        Installieren via: <code style={{ color: C.info }}>~/.agents/skills/install-skill.sh owner/repo</code>
      </span>
    </div>
  );

  return (
    <div className="space-y-2">
      {repos.map((repo) => {
        const isOpen = expanded[repo.name] ?? true;
        return (
          <div
            key={repo.name}
            className="rounded-xl overflow-hidden"
            style={{
              backgroundColor: C.bgElevated,
              border: "1px solid rgba(255,255,255,0.06)",
            }}
          >
            <button
              onClick={() => setExpanded((prev) => ({ ...prev, [repo.name]: !isOpen }))}
              className="w-full flex items-center gap-3 p-3 cursor-pointer"
            >
              <Github size={13} style={{ color: "var(--color-text-muted)", flexShrink: 0 }} />
              <div className="flex-1 text-left">
                <span className="text-xs font-medium" style={{ color: "var(--color-text-primary)" }}>
                  {repo.name}
                </span>
                <span className="ml-2 text-[10px]" style={{ color: "var(--color-text-muted)" }}>
                  {repo.source} · {repo.version}
                </span>
              </div>
              <span className="text-[10px] px-1.5 py-0.5 rounded" style={{ backgroundColor: `${C.accent}1F`, color: C.accentHover }}>
                {repo.skills.length} Skills
              </span>
              {isOpen ? <ChevronDown size={12} style={{ color: "var(--color-text-muted)" }} /> : <ChevronRight size={12} style={{ color: "var(--color-text-muted)" }} />}
            </button>
            {isOpen && repo.skills.length > 0 && (
              <div
                className="px-3 pb-3 flex flex-wrap gap-1.5"
                style={{ borderTop: "1px solid rgba(255,255,255,0.04)" }}
              >
                {repo.skills.map((skill) => (
                  <span
                    key={skill}
                    className="text-[10px] px-2 py-0.5 rounded-full"
                    style={{
                      backgroundColor: "rgba(255,255,255,0.04)",
                      border: "1px solid rgba(255,255,255,0.08)",
                      color: "var(--color-text-secondary)",
                    }}
                  >
                    {skill}
                  </span>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function PluginMatrix() {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<PluginAssignment | null>(null);

  const { data: agentsData } = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.agents.list(),
    staleTime: 30_000,
  });

  const { data: pluginsData } = useQuery({
    queryKey: ["cli-plugins"],
    queryFn: () => api.plugins.list(),
    staleTime: 30_000,
  });

  const cliAgents = useMemo(
    () =>
      ((agentsData ?? []) as Agent[])
        .filter((a) => a.agent_runtime === "cli-bridge" && a.provision_status !== "offline")
        .sort((a, b) => a.name.localeCompare(b.name)),
    [agentsData]
  );
  const plugins: CliPlugin[] = pluginsData?.plugins ?? [];

  const savedState = useMemo(() => {
    const state: PluginAssignment = {};
    for (const agent of cliAgents) {
      state[agent.id] = new Set(
        agent.cli_plugins ?? plugins.map((p) => p.key)
      );
    }
    return state;
  }, [cliAgents, plugins]);

  const currentState = draft ?? savedState;

  const isDirty = useMemo(() => {
    if (!draft) return false;
    for (const agentId of Object.keys(draft)) {
      const saved = savedState[agentId] ?? new Set<string>();
      const current = draft[agentId] ?? new Set<string>();
      if (saved.size !== current.size) return true;
      for (const k of saved) {
        if (!current.has(k)) return true;
      }
    }
    return false;
  }, [draft, savedState]);

  const handleToggle = (agentId: string, pluginKey: string) => {
    const base: PluginAssignment = draft
      ? { ...draft }
      : Object.fromEntries(
          Object.entries(savedState).map(([id, set]) => [id, new Set(set)])
        );
    const agentSet = new Set(
      base[agentId] ?? savedState[agentId] ?? new Set<string>()
    );
    if (agentSet.has(pluginKey)) {
      agentSet.delete(pluginKey);
    } else {
      agentSet.add(pluginKey);
    }
    setDraft({ ...base, [agentId]: agentSet });
  };

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (!draft) return;
      const promises = [];
      for (const agent of cliAgents) {
        const saved = savedState[agent.id];
        const current = draft[agent.id];
        if (!current) continue;
        const hasChanged =
          saved.size !== current.size ||
          [...saved].some((k) => !current.has(k));
        if (hasChanged) {
          const arr = [...current];
          promises.push(
            api.skills.setAgentSkills(agent.id, {
              update_cli_plugins: true,
              cli_plugins: arr.length > 0 ? arr : null,
            })
          );
        }
      }
      await Promise.all(promises);
    },
    onSuccess: () => {
      setDraft(null);
      qc.invalidateQueries({ queryKey: ["agents"] });
      qc.invalidateQueries({ queryKey: ["cli-plugins"] });
      notify.success("Team-Zuweisungen gespeichert");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  if (!cliAgents.length && !plugins.length) {
    return (
      <div className="space-y-6">
        <div className="text-xs text-center py-8 text-[var(--color-text-muted)]">
          Keine CLI-Bridge Agents oder Plugins gefunden
        </div>
        <div>
          <h3 className="text-xs font-medium mb-3" style={{ color: "var(--color-text-muted)" }}>
            GitHub Skill-Repos
          </h3>
          <GithubSkillsSection />
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {isDirty && (
        <div
          className="flex items-center justify-between p-3 rounded-xl"
          style={{
            backgroundColor: `${C.accent}14`,
            border: `1px solid ${C.accent}33`,
          }}
        >
          <span className="text-xs" style={{ color: C.accent }}>
            Ungespeicherte Aenderungen
          </span>
          <div className="flex gap-2">
            <button
              onClick={() => setDraft(null)}
              className="text-xs px-2.5 py-1.5 rounded-lg cursor-pointer flex items-center gap-1"
              style={{
                color: "var(--color-text-muted)",
                backgroundColor: "rgba(255,255,255,0.04)",
                border: "1px solid rgba(255,255,255,0.07)",
              }}
            >
              <Undo2 size={12} />
              Verwerfen
            </button>
            <button
              onClick={() => saveMutation.mutate()}
              disabled={saveMutation.isPending}
              className="text-xs px-3 py-1.5 rounded-lg cursor-pointer flex items-center gap-1"
              style={{ backgroundColor: C.accent, color: C.textPrimary }}
            >
              {saveMutation.isPending ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <Save size={12} />
              )}
              Speichern
            </button>
          </div>
        </div>
      )}

      <div
        className="overflow-x-auto rounded-xl"
        style={{
          backgroundColor: C.bgElevated,
          border: "1px solid rgba(255,255,255,0.06)",
          overscrollBehaviorX: "contain",
        } as React.CSSProperties}
        tabIndex={0}
        role="region"
        aria-label="Plugin Team-Zuweisungen"
      >
        <table className="w-full text-xs">
          <thead>
            <tr>
              <th
                className="sticky left-0 z-10 p-3 text-left font-medium"
                style={{
                  backgroundColor: C.bgElevated,
                  color: "var(--color-text-muted)",
                }}
              >
                Plugin
              </th>
              {cliAgents.map((agent) => (
                <th
                  key={agent.id}
                  className="p-3 text-center font-medium min-w-[80px]"
                  style={{ color: "var(--color-text-secondary)" }}
                >
                  <span className="text-base">{agent.emoji}</span>
                  <br />
                  <span className="text-[10px]">{agent.name}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {plugins.map((plugin) => (
              <tr
                key={plugin.key}
                className="border-t"
                style={{ borderColor: "rgba(255,255,255,0.04)" }}
              >
                <td
                  className="sticky left-0 z-10 p-3"
                  style={{
                    backgroundColor: C.bgElevated,
                    color: "var(--color-text-primary)",
                  }}
                >
                  <span className="font-medium">{plugin.name}</span>
                  <span
                    className="ml-1.5 text-[10px]"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    {plugin.version}
                  </span>
                </td>
                {cliAgents.map((agent) => {
                  const isActive = (
                    currentState[agent.id] ?? new Set<string>()
                  ).has(plugin.key);
                  return (
                    <td key={agent.id} className="p-3 text-center">
                      <button
                        onClick={() => handleToggle(agent.id, plugin.key)}
                        className="w-5 h-5 rounded cursor-pointer flex items-center justify-center mx-auto transition-colors"
                        style={{
                          backgroundColor: isActive
                            ? `${C.accent}33`
                            : "rgba(255,255,255,0.04)",
                          border: `1px solid ${
                            isActive
                              ? `${C.accent}66`
                              : "rgba(255,255,255,0.08)"
                          }`,
                        }}
                      >
                        {isActive && (
                          <Check size={12} style={{ color: C.accent }} />
                        )}
                      </button>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* GitHub Skill-Repos */}
      <div>
        <h3 className="text-xs font-medium mb-3" style={{ color: "var(--color-text-muted)" }}>
          GitHub Skill-Repos
        </h3>
        <GithubSkillsSection />
      </div>
    </div>
  );
}

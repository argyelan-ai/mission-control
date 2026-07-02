"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Plus, X } from "lucide-react";
import { api } from "@/lib/api";
import type { VllmContainer } from "@/lib/types";
import { C, STATUS_TEXT } from "@/lib/colors";

function VllmContainerCard({ container, onAdd }: { container: VllmContainer; onAdd: () => void }) {
  return (
    <div
      className="flex items-center gap-3 px-4 py-3 rounded-xl"
      style={{ border: `1px solid ${C.borderSubtle}`, background: "rgba(255,255,255,0.02)" }}
    >
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-medium" style={{ color: C.textPrimary }}>
            {container.container_name}
          </span>
          <span className="text-xs truncate" style={{ color: C.textMuted }}>
            {container.image}
          </span>
        </div>
        <div className="text-xs mt-0.5" style={{ color: C.textMuted }}>
          {container.endpoint || "Endpoint nicht erkannt"}
        </div>
      </div>
      <button
        onClick={onAdd}
        className="flex items-center gap-1 text-xs px-3 py-1.5 rounded-lg cursor-pointer transition-colors"
        style={{
          color: C.info,
          border: `1px solid ${C.info}4D`,
          background: `${C.info}0F`,
        }}
      >
        <Plus size={11} />
        Hinzufügen
      </button>
    </div>
  );
}

const VLLM_TAG_OPTIONS = ["coder", "general", "planner", "lead", "fallback", "reviewer"];

function AddVllmModal({
  container,
  onClose,
  onAdded,
}: {
  container: VllmContainer;
  onClose: () => void;
  onAdded: () => void;
}) {
  const [displayName, setDisplayName] = useState(container.container_name);
  const [endpoint, setEndpoint] = useState(container.endpoint);
  const [roleTags, setRoleTags] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const addMutation = useMutation({
    mutationFn: () =>
      api.runtimes.vllm.add({
        container_name: container.container_name,
        display_name: displayName.trim() || container.container_name,
        endpoint: endpoint.trim(),
        role_tags: roleTags,
      }),
    onSuccess: onAdded,
    onError: (e: Error) => setError(e.message || "Fehler beim Hinzufügen"),
  });

  const toggleTag = (tag: string) => {
    setRoleTags((prev) => (prev.includes(tag) ? prev.filter((t) => t !== tag) : [...prev, tag]));
  };

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!endpoint.trim()) {
      setError("Endpoint darf nicht leer sein");
      return;
    }
    addMutation.mutate();
  };

  const inputStyle: React.CSSProperties = {
    background: "rgba(255,255,255,0.03)",
    border: `1px solid ${C.borderSubtle}`,
    color: C.textPrimary,
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: "rgba(0,0,0,0.6)" }}
      onClick={onClose}
    >
      <form
        onSubmit={submit}
        className="w-full max-w-md p-5 rounded-xl"
        onClick={(e) => e.stopPropagation()}
        style={{
          background: C.bgElevated,
          border: `1px solid ${C.borderSubtle}`,
          boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
        }}
      >
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-semibold" style={{ color: C.textPrimary }}>
            vLLM Runtime hinzufügen
          </h3>
          <button
            type="button"
            onClick={onClose}
            className="p-1 rounded cursor-pointer"
            style={{ color: C.textMuted }}
            aria-label="Schließen"
          >
            <X size={14} />
          </button>
        </div>

        <div className="text-xs mb-3" style={{ color: C.textMuted }}>
          Container: <span style={{ color: C.textPrimary }}>{container.container_name}</span>
        </div>

        <label className="block mb-3">
          <span className="text-xs block mb-1" style={{ color: C.textMuted }}>
            Display-Name
          </span>
          <input
            type="text"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            className="w-full px-3 py-2 rounded-lg text-sm"
            style={inputStyle}
          />
        </label>

        <label className="block mb-3">
          <span className="text-xs block mb-1" style={{ color: C.textMuted }}>
            Endpoint
          </span>
          <input
            type="text"
            value={endpoint}
            onChange={(e) => setEndpoint(e.target.value)}
            className="w-full px-3 py-2 rounded-lg text-sm font-mono"
            style={inputStyle}
            placeholder="http://192.0.2.10:8003/v1"
          />
        </label>

        <div className="mb-4">
          <span className="text-xs block mb-2" style={{ color: C.textMuted }}>
            Rollen-Tags (optional)
          </span>
          <div className="flex flex-wrap gap-1.5">
            {VLLM_TAG_OPTIONS.map((tag) => {
              const active = roleTags.includes(tag);
              return (
                <button
                  key={tag}
                  type="button"
                  onClick={() => toggleTag(tag)}
                  className="text-xs px-2.5 py-1 rounded-full cursor-pointer transition-colors"
                  style={{
                    color: active ? C.info : C.textMuted,
                    border: `1px solid ${active ? `${C.info}66` : C.borderSubtle}`,
                    background: active ? `${C.info}1A` : "rgba(255,255,255,0.02)",
                  }}
                >
                  {tag}
                </button>
              );
            })}
          </div>
        </div>

        {error && (
          <div
            className="text-xs px-3 py-2 rounded-lg mb-3"
            style={{
              color: STATUS_TEXT.error,
              background: `${C.error}0F`,
              border: `1px solid ${C.error}26`,
            }}
          >
            {error}
          </div>
        )}

        <div className="flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="text-xs px-3 py-1.5 rounded-lg cursor-pointer"
            style={{ color: C.textMuted, border: `1px solid ${C.borderSubtle}` }}
          >
            Abbrechen
          </button>
          <button
            type="submit"
            disabled={addMutation.isPending}
            className="text-xs px-3 py-1.5 rounded-lg cursor-pointer flex items-center gap-1.5"
            style={{
              color: C.info,
              border: `1px solid ${C.info}66`,
              background: `${C.info}1A`,
              opacity: addMutation.isPending ? 0.6 : 1,
            }}
          >
            {addMutation.isPending && <Loader2 size={11} className="animate-spin" />}
            Hinzufügen
          </button>
        </div>
      </form>
    </div>
  );
}

export function VllmContainerCatalog() {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState<VllmContainer | null>(null);

  const { data } = useQuery({
    queryKey: ["vllm-discover"],
    queryFn: () => api.runtimes.vllm.discover(),
    refetchInterval: 30_000,
  });

  const unregistered = (data?.containers ?? []).filter((c) => !c.is_registered);
  if (unregistered.length === 0) return null;

  return (
    <>
      <div className="flex items-center gap-2 mb-2 px-0.5">
        <span
          className="text-xs font-medium tracking-wider uppercase"
          style={{ color: C.textMuted, letterSpacing: "0.07em", fontSize: "10px" }}
        >
          Erkannt
        </span>
        <div className="flex-1 h-px" style={{ background: C.border }} />
      </div>
      <div className="flex flex-col gap-2 mb-3">
        {unregistered.map((c) => (
          <VllmContainerCard key={c.container_name} container={c} onAdd={() => setOpen(c)} />
        ))}
      </div>
      {open && (
        <AddVllmModal
          container={open}
          onClose={() => setOpen(null)}
          onAdded={() => {
            queryClient.invalidateQueries({ queryKey: ["runtimes"] });
            queryClient.invalidateQueries({ queryKey: ["vllm-discover"] });
            setOpen(null);
          }}
        />
      )}
    </>
  );
}

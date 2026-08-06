"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Plus, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { api } from "@/lib/api";
import type { VllmContainer } from "@/lib/types";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { C, STATUS_TEXT } from "@/lib/colors";
import { ListRow, MetaChip, MetaText } from "@/components/shared/ListRow";

function VllmContainerCard({ container, onAdd }: { container: VllmContainer; onAdd: () => void }) {
  const t = useTranslations("runtimes.vllmCatalog");
  // A discovered container is a list row like every other list row on this
  // page — it just happens to be rare, because it only appears while something
  // is running that Mission Control does not know about yet.
  const running = container.state === "running";
  return (
    <ListRow
      testId="vllm-container-row"
      dataAttrs={{ "data-container": container.container_name }}
      tone={running ? "ok" : "idle"}
      name={container.container_name}
      summary={[container.state, container.image].filter(Boolean).join(" · ")}
      chips={<MetaChip tone={running ? "ok" : "idle"}>{container.state}</MetaChip>}
      meta={
        <>
          <MetaText mono title={container.image}>
            {container.image}
          </MetaText>
          <MetaText mono title={container.endpoint || undefined}>
            {container.endpoint || t("endpointNotDetected")}
          </MetaText>
        </>
      }
      action={
        <button
          onClick={onAdd}
          className="flex items-center gap-1 text-xs px-2.5 py-2 sm:py-1.5 min-h-11 sm:min-h-0 rounded-md cursor-pointer transition-colors"
          style={{
            color: C.info,
            border: `1px solid ${C.info}4D`,
            background: "transparent",
          }}
        >
          <Plus size={11} />
          {t("add")}
        </button>
      }
    />
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
  const t = useTranslations("runtimes.vllmCatalog");
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
    onError: (e: Error) => setError(e.message || t("addFailed")),
  });

  const toggleTag = (tag: string) => {
    setRoleTags((prev) => (prev.includes(tag) ? prev.filter((rt) => rt !== tag) : [...prev, tag]));
  };

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!endpoint.trim()) {
      setError(t("endpointRequired"));
      return;
    }
    addMutation.mutate();
  };

  const inputStyle: React.CSSProperties = {
    background: "var(--color-bg-surface)",
    border: `1px solid ${C.borderSubtle}`,
    color: C.textPrimary,
  };

  return (
    <ResponsiveModal open onClose={onClose} aria-label={t("modalTitle")} className="sm:max-w-md">
      <form
        onSubmit={submit}
        className="w-full p-5 overflow-y-auto"
      >
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-semibold" style={{ color: C.textPrimary }}>
            {t("modalTitle")}
          </h3>
          <button
            type="button"
            onClick={onClose}
            className="p-1 rounded cursor-pointer"
            style={{ color: C.textMuted }}
            aria-label={t("close")}
          >
            <X size={14} />
          </button>
        </div>

        <div className="text-xs mb-3" style={{ color: C.textMuted }}>
          {t("container")} <span style={{ color: C.textPrimary }}>{container.container_name}</span>
        </div>

        <label className="block mb-3">
          <span className="text-xs block mb-1" style={{ color: C.textMuted }}>
            {t("displayName")}
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
            {t("endpoint")}
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
            {t("roleTagsOptional")}
          </span>
          <div className="flex flex-wrap gap-1.5">
            {VLLM_TAG_OPTIONS.map((tag) => {
              const active = roleTags.includes(tag);
              return (
                <button
                  key={tag}
                  type="button"
                  onClick={() => toggleTag(tag)}
                  className="font-mono text-[11px] px-2.5 py-1 rounded-sm cursor-pointer transition-colors"
                  style={{
                    color: active ? C.info : C.textMuted,
                    border: `1px solid ${active ? `${C.info}66` : C.borderSubtle}`,
                    background: active ? `${C.info}1A` : "var(--color-bg-hover)",
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
            {t("cancel")}
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
            {t("add")}
          </button>
        </div>
      </form>
    </ResponsiveModal>
  );
}

export function VllmContainerCatalog() {
  const t = useTranslations("runtimes.vllmCatalog");
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
      <div className="label-sys mb-1.5">{t("discovered")}</div>
      <div className="flex flex-col gap-1.5 mb-3">
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

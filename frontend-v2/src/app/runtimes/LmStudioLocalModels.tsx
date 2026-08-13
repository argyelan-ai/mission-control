"use client";

/**
 * LM Studio models that are loaded in the LM Studio app but have no matching
 * `Runtime` row configured in Mission Control yet (`unattachedModels`).
 *
 * This is the pre-Task-5 page.tsx's `LMStudioModelCard` + its Active/Inactive
 * filtering logic, ported verbatim (source: `git show 6774d479` — the last
 * commit before the slot-stage page rewrite) and re-homed into the Models
 * section's "local" tab per the design doc: "LMStudioModelCard load/unload
 * stays available via the Models section, not on the main view."
 *
 * `ActionButton`/`RowNote` are copied here rather than shared with
 * `RuntimeDetailPanel.tsx`'s own (visually different) `ActionButton` —
 * that file already made the same call for its own surface; each leaf here
 * keeps its own copy rather than inventing a premature shared component.
 */

import { useState } from "react";
import { useTranslations } from "next-intl";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Play, Square, Settings2, Loader2, type LucideIcon } from "lucide-react";
import { api } from "@/lib/api";
import type { LMStudioModel } from "@/lib/types";
import { C, STATUS_TEXT } from "@/lib/colors";
import { fmtCtx } from "@/lib/utils";
import { ListRow, MetaChip, MetaText } from "@/components/shared/ListRow";
import { OverflowMenu } from "@/components/shared/OverflowMenu";
import { ContextSettingsPanel, loadStoredCtx } from "./ContextSettings";

// ── Action Button (verbatim from the pre-Task-5 page.tsx) ────────────────────

function ActionButton({
  icon: Icon,
  label,
  disabled,
  onClick,
  loading,
  variant,
}: {
  icon: LucideIcon;
  label: string;
  disabled: boolean;
  onClick: () => void;
  loading: boolean;
  variant: "success" | "danger" | "default";
}) {
  // Outlined, not filled. A filled error-red Stop button on every running
  // runtime made red the loudest colour on a page where nothing is wrong —
  // and red already means "failed" elsewhere. The hue still carries the
  // action; the fill only appears on hover.
  const colors = {
    success: { border: `${C.online}40`, text: C.online, hover: `${C.online}1A` },
    danger:  { border: `${C.error}33`, text: STATUS_TEXT.error, hover: `${C.error}1A` },
    default: { border: C.borderActive, text: C.textMuted, hover: C.bgHover },
  };
  const c = colors[variant];

  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={label}
      aria-label={label}
      className="flex items-center justify-center w-11 h-11 sm:w-7 sm:h-7 min-w-11 sm:min-w-[28px] cursor-pointer disabled:cursor-not-allowed"
    >
      <span
        aria-hidden
        className="action-btn flex items-center justify-center w-7 h-7 rounded-md transition-colors"
        style={{
          background: "transparent",
          border: `1px solid ${disabled ? C.borderSubtle : c.border}`,
          color: disabled ? C.textDim : c.text,
          ["--action-hover" as string]: c.hover,
        }}
      >
        {loading ? <Loader2 size={12} className="animate-spin" /> : <Icon size={12} />}
      </span>
    </button>
  );
}

/** The one note style under a row (action feedback). Verbatim. */
function RowNote({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="text-[11px] px-2.5 py-1.5 rounded-md"
      style={{
        background: C.accentSubtle,
        border: `1px solid ${C.borderAccent}`,
        color: C.textSecondary,
      }}
    >
      {children}
    </div>
  );
}

// ── LM Studio Model Row (verbatim) ────────────────────────────────────────────

function LMStudioModelCard({ model }: { model: LMStudioModel }) {
  const t = useTranslations("runtimes");
  const queryClient = useQueryClient();
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const storedCtx = loadStoredCtx(model.id);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["lmstudio-models"] });
  };

  const loadMutation = useMutation({
    mutationFn: () => api.lmstudio.load(model.id, storedCtx ?? undefined),
    onSuccess: (data) => { setActionMsg(data.message); invalidate(); },
    onError: () => setActionMsg(t("loadFailed")),
  });

  const unloadMutation = useMutation({
    mutationFn: () => api.lmstudio.unload(model.id),
    onSuccess: (data) => { setActionMsg(data.message); invalidate(); },
    onError: () => setActionMsg(t("unloadFailed")),
  });

  const isMutating = loadMutation.isPending || unloadMutation.isPending;

  return (
    <div className="flex flex-col gap-1.5">
      <ListRow
        testId="lms-model-row"
        dataAttrs={{ "data-model": model.id }}
        tone={model.is_loaded ? "ok" : "idle"}
        name={model.display_name}
        summary={[
          model.is_loaded ? t("states.ready") : t("states.stopped"),
          "LM Studio",
          model.size_gb > 0 ? `${model.size_gb.toFixed(1)} GB` : null,
        ]
          .filter(Boolean)
          .join(" · ")}
        chips={
          <>
            {model.is_embedding && <MetaChip tone="idle">EMBED</MetaChip>}
            <MetaChip tone="idle">LM Studio</MetaChip>
            {storedCtx && (
              <MetaChip tone="idle" className="tabular-nums">
                {fmtCtx(storedCtx)} ctx
              </MetaChip>
            )}
          </>
        }
        meta={
          model.size_gb > 0 ? (
            <MetaText className="tabular-nums shrink-0">{model.size_gb.toFixed(1)} GB</MetaText>
          ) : undefined
        }
        action={
          model.is_loaded ? (
            <ActionButton
              icon={Square}
              label={t("unload")}
              disabled={isMutating}
              onClick={() => unloadMutation.mutate()}
              loading={unloadMutation.isPending}
              variant="danger"
            />
          ) : (
            <ActionButton
              icon={Play}
              label={t("load")}
              disabled={isMutating}
              onClick={() => loadMutation.mutate()}
              loading={loadMutation.isPending}
              variant="success"
            />
          )
        }
        overflow={
          !model.is_embedding ? (
            <OverflowMenu
              label={t("moreActions", { name: model.display_name })}
              testId={`lms-more-${model.id}`}
              actions={[
                {
                  id: "ctx",
                  label: t("ctxSettings"),
                  icon: Settings2,
                  onClick: () => setSettingsOpen((o) => !o),
                },
              ]}
            />
          ) : undefined
        }
      />

      {settingsOpen && !model.is_embedding && (
        <ContextSettingsPanel
          modelId={model.id}
          initialCtx={storedCtx}
          onClose={() => setSettingsOpen(false)}
        />
      )}

      {actionMsg && <RowNote>{actionMsg}</RowNote>}
    </div>
  );
}

// ── Root — unattached models list ─────────────────────────────────────────────

export function LmStudioLocalModels() {
  const t = useTranslations("runtimes.models");

  // Same query keys page.tsx/ModelsSection already use — TanStack serves
  // these from cache, no extra request.
  const { data } = useQuery({
    queryKey: ["runtimes"],
    queryFn: () => api.runtimes.list(),
  });
  const { data: lmsData } = useQuery({
    queryKey: ["lmstudio-models"],
    queryFn: () => api.lmstudio.list(),
    refetchInterval: 15_000,
  });

  const lmsRuntimes = data?.runtimes.filter((rt) => rt.runtime_type === "lmstudio") ?? [];
  const configuredLmsIds = new Set(lmsRuntimes.map((r) => r.lms_identifier).filter(Boolean));
  const unattachedModels = (lmsData?.models ?? []).filter((m) => !configuredLmsIds.has(m.id));

  if (unattachedModels.length === 0) return null;

  return (
    <div className="mt-5">
      <div className="label-sys mb-1.5">{t("lmStudioLocalTitle")}</div>
      <div className="flex flex-col gap-1.5">
        {unattachedModels.map((m) => (
          <LMStudioModelCard key={m.id} model={m} />
        ))}
      </div>
    </div>
  );
}

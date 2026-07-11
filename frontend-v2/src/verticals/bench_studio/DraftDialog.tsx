"use client";

import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { C } from "@/lib/colors";
import { notify } from "@/lib/notify";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { benchApi } from "@/verticals/bench_studio/api";
import type { BenchChallenge } from "./types";

const MAX = 280;
const WARN_AT = 260;

export function DraftDialog({
  challenge,
  open,
  onClose,
}: {
  challenge: BenchChallenge;
  open: boolean;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const [speedLabels, setSpeedLabels] = useState(false);

  // Reset state each time the dialog opens so a second open starts clean.
  useEffect(() => {
    if (open) {
      setText("");
      setSpeedLabels(false);
    }
  }, [open]);

  const counterColor =
    text.length > MAX ? C.error : text.length > WARN_AT ? C.warning : C.textMuted;

  const mutation = useMutation({
    mutationFn: () =>
      benchApi.challenges.draft(challenge.id, {
        tweet_text: text,
        include_speed_labels: speedLabels,
      }),
    onSuccess: (res) => {
      notify.success("Draft in der Inbox — warte auf Freigabe");
      res.warnings.forEach((w) => notify.info(w));
      qc.invalidateQueries({ queryKey: ["bench-challenge", challenge.id] });
      qc.invalidateQueries({ queryKey: ["bench-challenges"] });
      qc.invalidateQueries({ queryKey: ["approvals"] });
      onClose();
    },
    onError: () => notify.error("Draft konnte nicht erstellt werden"),
  });

  const disabled = text.trim().length === 0 || text.length > MAX || mutation.isPending;

  return (
    <ResponsiveModal open={open} onClose={onClose} aria-label="Draft erstellen">
      <div
        className="flex flex-col gap-4 p-5 rounded-xl w-full"
        style={{ backgroundColor: C.bgElevated, border: `1px solid ${C.border}` }}
      >
        <h3 className="text-base font-semibold" style={{ color: C.textPrimary }}>
          X-Post Draft — {challenge.title}
        </h3>

        <div className="flex flex-col gap-1.5">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={4}
            placeholder="Tweet-Text …"
            className="w-full rounded-lg p-3 text-sm resize-none outline-none"
            style={{
              backgroundColor: C.bgDeep,
              color: C.textPrimary,
              border: `1px solid ${C.border}`,
            }}
          />
          <span className="self-end text-xs font-mono tabular-nums" style={{ color: counterColor }}>
            {text.length}/{MAX}
          </span>
        </div>

        <label className="flex items-center gap-2 text-sm" style={{ color: C.textSecondary }}>
          <input
            type="checkbox"
            checked={speedLabels}
            onChange={(e) => setSpeedLabels(e.target.checked)}
          />
          Speed-Labels ins Grid einblenden (z. B. &quot;DeepSeek · 42 s · 87 tok/s&quot;)
        </label>

        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="px-3 py-1.5 rounded-lg text-sm"
            style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
          >
            Abbrechen
          </button>
          <button
            onClick={() => mutation.mutate()}
            disabled={disabled}
            className="px-3 py-1.5 rounded-lg text-sm font-medium disabled:opacity-40"
            style={{ backgroundColor: C.accentSubtle, color: C.accent, border: `1px solid ${C.borderAccent}` }}
          >
            Draft erstellen
          </button>
        </div>
      </div>
    </ResponsiveModal>
  );
}

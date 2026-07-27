"use client";

/**
 * ConfirmDialog / PromptDialog — the v3 replacement for every browser-native
 * `confirm()` / `prompt()` (panel register rule 3). Small centered dialog on
 * all viewports (per M12), B2 chrome: accent top edge, mono micro-labels,
 * ghost cancel + danger/primary confirm key, backdrop without blur.
 *
 * Usage: keep an `open` state at the call site, render
 *   <ConfirmDialog open={!!x} title="Delete job" body={…} danger
 *     onConfirm={…} onCancel={…} />
 * PromptDialog adds a single text input (replaces `prompt()`).
 */

import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { AlertTriangle, X } from "lucide-react";
import { C } from "@/lib/colors";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";

interface BaseProps {
  open: boolean;
  /** Mono micro-label above the title (defaults to "Confirm"). */
  kicker?: string;
  title: string;
  body?: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** danger (default) = red key; false = primary accent key. */
  danger?: boolean;
  loading?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

function DialogShell({
  open,
  kicker = "Confirm",
  title,
  body,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  danger = true,
  loading = false,
  onConfirm,
  onCancel,
  children,
  confirmDisabled,
}: BaseProps & { children?: React.ReactNode; confirmDisabled?: boolean }) {
  useBodyScrollLock(open);

  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !loading) onCancel();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, loading, onCancel]);

  const accent = danger ? C.error : C.accent;

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.12 }}
          className="fixed inset-0 z-[100] flex items-center justify-center px-4"
          style={{ background: "rgba(5,4,3,0.8)" }}
          onClick={() => !loading && onCancel()}
        >
          <motion.div
            initial={{ opacity: 0, y: 12, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 8, scale: 0.98 }}
            transition={{ type: "spring", stiffness: 300, damping: 26 }}
            className="w-full max-w-md rounded-md flex flex-col overflow-hidden"
            style={{
              background: "var(--color-bg-elevated)",
              border: "1px solid var(--color-border)",
              boxShadow: "var(--shadow-elevated)",
            }}
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
            aria-label={title}
          >
            {/* v3 modal signature: 2px accent top edge */}
            <div className="h-[2px] w-full" style={{ background: danger ? C.error : C.accent }} />

            <div className="flex items-start justify-between gap-3 px-5 pt-4 pb-3">
              <div className="flex items-center gap-3 min-w-0">
                {danger && (
                  <span
                    className="shrink-0 w-8 h-8 rounded-sm flex items-center justify-center"
                    style={{ background: "rgba(239,68,68,0.10)", border: "1px solid rgba(239,68,68,0.25)" }}
                  >
                    <AlertTriangle size={15} style={{ color: C.error }} />
                  </span>
                )}
                <div className="min-w-0">
                  <div className="label-sys label-sys--dim">{kicker}</div>
                  <div
                    className="truncate text-[15px] font-semibold"
                    style={{ color: C.textPrimary }}
                    title={title}
                  >
                    {title}
                  </div>
                </div>
              </div>
              <button
                type="button"
                onClick={onCancel}
                disabled={loading}
                aria-label="Cancel"
                className="shrink-0 rounded-sm p-1 transition-colors hover:bg-[var(--color-bg-hover)] cursor-pointer disabled:opacity-40"
                style={{ color: C.textMuted }}
              >
                <X size={14} />
              </button>
            </div>

            {(body || children) && (
              <div className="px-5 pb-4 space-y-3 text-[13px] leading-relaxed" style={{ color: C.textSecondary }}>
                {body && <div>{body}</div>}
                {children}
              </div>
            )}

            <div
              className="flex items-center justify-end gap-2 px-5 py-3"
              style={{ borderTop: "1px solid var(--color-border-subtle)" }}
            >
              <button
                type="button"
                onClick={onCancel}
                disabled={loading}
                className="font-mono uppercase rounded-sm px-3 py-2 text-[10.5px] tracking-[0.14em] transition-colors cursor-pointer disabled:opacity-50"
                style={{
                  background: "transparent",
                  border: "1px solid var(--color-border)",
                  color: C.textSecondary,
                }}
              >
                {cancelLabel}
              </button>
              <button
                type="button"
                onClick={onConfirm}
                disabled={loading || confirmDisabled}
                className="font-mono uppercase rounded-sm px-3 py-2 text-[10.5px] tracking-[0.14em] transition-colors cursor-pointer disabled:opacity-50 flex items-center gap-1.5"
                style={
                  danger
                    ? { background: "rgba(239,68,68,0.14)", border: "1px solid rgba(239,68,68,0.45)", color: C.error }
                    : { background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.accent }
                }
              >
                {loading && (
                  <span
                    className="inline-block w-3 h-3 rounded-full border-[1.5px] border-t-transparent animate-spin"
                    style={{ borderColor: accent, borderTopColor: "transparent" }}
                  />
                )}
                {confirmLabel}
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

export function ConfirmDialog(props: BaseProps) {
  return <DialogShell {...props} />;
}

interface PromptDialogProps extends Omit<BaseProps, "onConfirm"> {
  inputLabel: string;
  placeholder?: string;
  defaultValue?: string;
  /** Return an error string to block submit, or null/undefined when valid. */
  validate?: (value: string) => string | null | undefined;
  onConfirm: (value: string) => void;
}

export function PromptDialog({
  inputLabel,
  placeholder,
  defaultValue = "",
  validate,
  onConfirm,
  open,
  ...rest
}: PromptDialogProps) {
  const [value, setValue] = useState(defaultValue);
  const [error, setError] = useState<string | null>(null);

  // Reset the field every time the dialog opens with a fresh defaultValue.
  useEffect(() => {
    if (open) {
      setValue(defaultValue);
      setError(null);
    }
  }, [open, defaultValue]);

  const submit = () => {
    const err = validate?.(value) ?? null;
    if (err) {
      setError(err);
      return;
    }
    onConfirm(value.trim());
  };

  return (
    <DialogShell
      {...rest}
      open={open}
      onConfirm={submit}
      confirmDisabled={rest.loading || value.trim().length === 0}
    >
      <div>
        <label className="label-sys label-sys--dim block mb-1.5">{inputLabel}</label>
        <input
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            setError(null);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
          }}
          placeholder={placeholder}
          autoFocus
          className="w-full px-2.5 py-2 rounded-md text-xs outline-none"
          style={{
            backgroundColor: "var(--color-bg-surface)",
            color: C.textPrimary,
            border: `1px solid ${error ? C.error : C.border}`,
          }}
        />
        {error && (
          <div className="mt-1.5 text-[11px]" style={{ color: C.error }}>
            {error}
          </div>
        )}
      </div>
    </DialogShell>
  );
}

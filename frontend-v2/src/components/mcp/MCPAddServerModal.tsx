"use client";

import { useState, useEffect } from "react";
import { useMutation } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { motion, AnimatePresence } from "framer-motion";
import { Server, X, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C } from "@/lib/colors";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";

interface Props {
  onClose: () => void;
  onSuccess: () => void;
}

const NAME_RE = /^[a-zA-Z0-9_-]+$/;

export function MCPAddServerModal({ onClose, onSuccess }: Props) {
  const t = useTranslations("skills.mcpAddModal");
  // iOS-safe scroll lock — always active while this component is mounted (M4)
  useBodyScrollLock(true);

  const [name, setName] = useState("");
  const [transport, setTransport] = useState<"stdio" | "http" | "sse">("stdio");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [url, setUrl] = useState("");
  const [description, setDescription] = useState("");

  // Escape key (mirrors InstallModal pattern)
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, [onClose]);

  const nameValid = name.length > 0 && NAME_RE.test(name);
  const nameInvalid = name.length > 0 && !nameValid;

  const urlValid = transport === "stdio" || url.trim().length > 0;
  const urlInvalid = transport !== "stdio" && url.length > 0 === false;

  const addMutation = useMutation({
    mutationFn: () =>
      api.mcpServers.create({
        name: name.trim(),
        transport,
        command: transport === "stdio" && command.trim() ? command.trim() : undefined,
        args:
          transport === "stdio" && args.trim()
            ? args.split(",").map((a) => a.trim()).filter(Boolean)
            : undefined,
        url: transport !== "stdio" && url.trim() ? url.trim() : undefined,
        description: description.trim() || undefined,
      }),
    onSuccess: () => {
      notify.success(t("added", { name }));
      onSuccess();
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const canSubmit = nameValid && urlValid && !addMutation.isPending;

  return (
    <AnimatePresence>
      <motion.div
        key="mcp-add-overlay"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.15 }}
        className="fixed inset-0 z-50 flex items-end sm:items-center justify-center sm:p-4 bg-black/75"
        style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
        onClick={onClose}
      >
        {/* Drag indicator — mobile only */}
        <div className="sm:hidden absolute bottom-[calc(92dvh-0.5rem)] left-1/2 -translate-x-1/2 z-10 w-8 h-1 rounded-full pointer-events-none" style={{ backgroundColor: "var(--color-bg-hover)" }} />

        <motion.div
          key="mcp-add-panel"
          initial={{ opacity: 0, y: 32 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: 32 }}
          transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
          className="relative w-full mx-2 rounded-t-2xl rounded-b-none sm:mx-0 sm:rounded-2xl overflow-hidden max-h-[92dvh] sm:max-h-[88vh] flex flex-col"
          role="dialog"
          aria-modal="true"
          aria-label={t("title")}
          style={{
            maxWidth: "min(520px, 100%)",
            background: C.bgBase,
            border: "1px solid var(--color-border)",
            boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
          }}
          onClick={(e) => e.stopPropagation()}
        >
        <div
          className="absolute inset-x-0 top-0 h-px pointer-events-none"
          style={{ background: "linear-gradient(90deg, transparent, var(--color-bg-hover) 50%, transparent)" }}
        />

        {/* Header */}
        <div
          className="flex items-center gap-3 px-5 py-4 shrink-0"
          style={{ borderBottom: "1px solid var(--color-border-subtle)" }}
        >
          <div
            className="w-8 h-8 rounded-xl flex items-center justify-center shrink-0"
            style={{ background: C.accentSubtle }}
          >
            <Server size={15} style={{ color: C.accent }} />
          </div>
          <div className="flex-1 min-w-0">
            <h2 className="text-sm font-semibold" style={{ color: "var(--color-text-primary)" }}>
              {t("title")}
            </h2>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg cursor-pointer"
            style={{ color: "var(--color-text-muted)" }}
            aria-label={t("close")}
          >
            <X size={16} />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-3">
          <label className="block text-xs">
            <span style={{ color: "var(--color-text-muted)" }}>{t("name")}</span>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t("namePlaceholder")}
              className="mt-1 w-full px-3 py-2 rounded-lg text-sm bg-transparent"
              style={{
                border: `1px solid ${nameInvalid ? `${C.error}66` : "var(--color-border)"}`,
                color: "var(--color-text-primary)",
              }}
              aria-label={t("name")}
            />
            {nameInvalid && (
              <span className="text-[11px]" style={{ color: C.error }}>
                {t("nameInvalid")}
              </span>
            )}
          </label>

          <label className="block text-xs">
            <span style={{ color: "var(--color-text-muted)" }}>{t("transport")}</span>
            <select
              value={transport}
              onChange={(e) => setTransport(e.target.value as typeof transport)}
              className="mt-1 w-full px-3 py-2 rounded-lg text-sm"
              style={{
                background: "var(--color-bg-surface)",
                border: "1px solid var(--color-border)",
                color: "var(--color-text-primary)",
              }}
              aria-label={t("transport")}
            >
              <option value="stdio">stdio</option>
              <option value="http">http</option>
              <option value="sse">sse</option>
            </select>
          </label>

          {transport === "stdio" && (
            <>
              <label className="block text-xs">
                <span style={{ color: "var(--color-text-muted)" }}>{t("command")}</span>
                <input
                  type="text"
                  value={command}
                  onChange={(e) => setCommand(e.target.value)}
                  placeholder={t("commandPlaceholder")}
                  className="mt-1 w-full px-3 py-2 rounded-lg text-sm font-mono bg-transparent"
                  style={{ border: "1px solid var(--color-border)", color: "var(--color-text-primary)" }}
                  aria-label={t("command")}
                />
              </label>
              <label className="block text-xs">
                <span style={{ color: "var(--color-text-muted)" }}>{t("args")}</span>
                <input
                  type="text"
                  value={args}
                  onChange={(e) => setArgs(e.target.value)}
                  placeholder={t("argsPlaceholder")}
                  className="mt-1 w-full px-3 py-2 rounded-lg text-sm font-mono bg-transparent"
                  style={{ border: "1px solid var(--color-border)", color: "var(--color-text-primary)" }}
                  aria-label={t("args")}
                />
              </label>
            </>
          )}

          {transport !== "stdio" && (
            <label className="block text-xs">
              <span style={{ color: "var(--color-text-muted)" }}>{t("url")}</span>
              <input
                type="text"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder={t("urlPlaceholder")}
                className="mt-1 w-full px-3 py-2 rounded-lg text-sm font-mono bg-transparent"
                style={{
                  border: `1px solid ${urlInvalid ? `${C.error}66` : "var(--color-border)"}`,
                  color: "var(--color-text-primary)",
                }}
                aria-label={t("url")}
              />
              {urlInvalid && (
                <span className="text-[11px]" style={{ color: C.error }}>
                  {t("urlInvalid")}
                </span>
              )}
            </label>
          )}

          <label className="block text-xs">
            <span style={{ color: "var(--color-text-muted)" }}>{t("description")}</span>
            <input
              type="text"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t("descriptionPlaceholder")}
              className="mt-1 w-full px-3 py-2 rounded-lg text-sm bg-transparent"
              style={{ border: "1px solid var(--color-border)", color: "var(--color-text-primary)" }}
              aria-label={t("description")}
            />
          </label>
        </div>

        {/* Footer */}
        <div
          className="flex justify-end gap-2 px-5 py-3 shrink-0"
          style={{ borderTop: "1px solid var(--color-border-subtle)", paddingBottom: "calc(env(safe-area-inset-bottom) + 0.75rem)" }}
        >
          <button
            onClick={onClose}
            className="px-3 py-1.5 rounded-lg text-xs cursor-pointer"
            style={{ color: "var(--color-text-muted)" }}
          >
            {t("cancel")}
          </button>
          <button
            onClick={() => addMutation.mutate()}
            disabled={!canSubmit}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
            style={{
              backgroundColor: C.accentSubtle,
              color: C.accent,
              border: `1px solid ${C.borderAccent}`,
            }}
          >
            {addMutation.isPending && <Loader2 size={12} className="animate-spin" />}
            {t("add")}
          </button>
        </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}

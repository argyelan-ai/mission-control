"use client";

/**
 * SshProcessDeployDialog — eine Host-Engine per Klick installieren und starten (PR 6).
 *
 * Der sparkrun-Deploy im LocalModelBrowser ist ein Recipe-Switch auf einer
 * bestehenden Runtime. Eine ssh_process-Engine hat noch gar keine Runtime-Zeile
 * und liegt beim ersten Mal auch noch nicht auf der Box — deshalb zwei
 * Schritte statt einem:
 *
 *   1 Installieren   — Hintergrund-Job auf der Box (Klonen, Bauen, ~110 GiB
 *                      Gewichte). Läuft Stunden, deshalb Live-Log mit Cursor
 *                      statt Spinner. Idempotent: ein zweiter Lauf holt nur
 *                      Fehlendes nach.
 *   2 Anlegen+Starten — legt die Runtime-Zeile an (POST /runtimes) und startet
 *                      sie über den BESTEHENDEN Start-Endpoint. Kein zweiter
 *                      Lifecycle-Pfad, exakt wie im BoxWizard.
 *
 * Das Kommando rendert das Backend (services/launch_template) — hier wird kein
 * Shell-String zusammengebaut. Zwei Renderer, die auseinanderlaufen, hätten
 * den Befehl auf Marks Box als Verlierer.
 *
 * Die Exklusivitäts-Warnung ist kein Schmuck: das Backend stoppt beim Start
 * jede andere exklusive Runtime derselben Box. Wer hier klickt, soll vorher
 * lesen, was dabei ausgeht.
 */

import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  Loader2,
  Rocket,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import type {
  Host,
  HostBootstrapLogLine,
  LocalRecipe,
  RecipeInstallLog,
  Runtime,
} from "@/lib/types";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";

/** ds4 und Verwandte hören auf 8888; abweichende Ports trägt der Operator ein. */
const DEFAULT_PORT = 8888;

export function extractApiError(err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err);
  const jsonStart = msg.indexOf("{");
  if (jsonStart >= 0) {
    try {
      const parsed = JSON.parse(msg.slice(jsonStart));
      if (typeof parsed.detail === "string") return parsed.detail;
    } catch {
      /* raw message */
    }
  }
  return msg;
}

/** "DeepSeek V4 Flash (ds4)" → "deepseek-v4-flash-ds4" */
export function slugify(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

/**
 * Was beim Start dieser Runtime gestoppt würde: jede andere aktivierte
 * Runtime mit exclusive_memory auf derselben Box. Genau die Regel, die
 * runtime_manager.ensure_exclusive_host durchsetzt — hier nur vorgelesen.
 */
export function exclusiveNeighbours(runtimes: Runtime[], hostId: string | null): Runtime[] {
  if (!hostId) return [];
  return runtimes.filter(
    (rt) => rt.exclusive_memory === true && rt.enabled !== false && rt.host?.id === hostId,
  );
}

export function SshProcessDeployDialog({
  recipe,
  onClose,
}: {
  recipe: LocalRecipe;
  onClose: () => void;
}) {
  const t = useTranslations("runtimes.localRegistry");
  const queryClient = useQueryClient();
  useBodyScrollLock(true);

  const [hostId, setHostId] = useState<string>("");
  const [port, setPort] = useState<number>(DEFAULT_PORT);
  const [runtimeSlug, setRuntimeSlug] = useState<string>(recipe.slug);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState(false);
  const [log, setLog] = useState<HostBootstrapLogLine[]>([]);
  const [status, setStatus] = useState<RecipeInstallLog["status"]>("idle");
  const cursor = useRef(0);
  const logEnd = useRef<HTMLDivElement | null>(null);

  const hostsQuery = useQuery({ queryKey: ["hosts"], queryFn: () => api.hosts.list() });
  const runtimesQuery = useQuery({ queryKey: ["runtimes"], queryFn: () => api.runtimes.list() });

  const sshHosts: Host[] = (hostsQuery.data ?? []).filter((h) => h.kind === "ssh");
  const host = sshHosts.find((h) => h.id === hostId) ?? null;

  useEffect(() => {
    if (!hostId && sshHosts.length > 0) setHostId(sshHosts[0].id);
  }, [hostId, sshHosts]);

  // Beim Öffnen einmal den Stand abholen: eine Installation, die in einer
  // früheren Sitzung lief, ist hier sonst unsichtbar.
  useEffect(() => {
    if (!hostId) return;
    let cancelled = false;
    cursor.current = 0;
    setLog([]);
    api.localRegistry
      .installLog(recipe.slug, hostId, 0)
      .then((res) => {
        if (cancelled) return;
        cursor.current = res.cursor;
        setLog(res.lines);
        setStatus(res.status);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [hostId, recipe.slug]);

  const polling = status === "running";

  useEffect(() => {
    if (!polling || !hostId) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const res = await api.localRegistry.installLog(recipe.slug, hostId, cursor.current);
        if (cancelled) return;
        cursor.current = res.cursor;
        if (res.lines.length > 0) setLog((prev) => [...prev, ...res.lines]);
        setStatus(res.status);
      } catch (err) {
        if (!cancelled) setError(extractApiError(err));
      }
    };
    // Sofort einmal, dann im Takt: sonst starrt man nach dem Klick drei
    // Sekunden auf ein leeres Log und weiss nicht, ob etwas passiert.
    poll();
    const timer = setInterval(poll, 3000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [polling, hostId, recipe.slug]);

  useEffect(() => {
    // Optional call: jsdom has no scrollIntoView, and an auto-scroll is not
    // worth an unhandled error in every test that renders a log.
    logEnd.current?.scrollIntoView?.({ block: "nearest" });
  }, [log.length]);

  const neighbours = exclusiveNeighbours(runtimesQuery.data?.runtimes ?? [], hostId || null);

  async function startInstall() {
    if (!hostId) return;
    setBusy(true);
    setError(null);
    setLog([]);
    cursor.current = 0;
    try {
      await api.localRegistry.install(recipe.slug, {
        host_id: hostId,
        port,
        ctx: recipe.context_len ?? undefined,
      });
      setStatus("running");
    } catch (err) {
      setError(extractApiError(err));
    } finally {
      setBusy(false);
    }
  }

  async function createAndStart() {
    if (!host) return;
    setBusy(true);
    setError(null);
    try {
      const rendered = await api.hosts.launchCommand({
        engine: recipe.engine,
        model_identifier: recipe.model_identifier,
        slug: runtimeSlug,
        port,
        launch_template: recipe.launch_template,
        stop_template: recipe.stop_template,
        ctx: recipe.context_len,
      });
      const runtime = await api.runtimes.create({
        slug: runtimeSlug,
        display_name: recipe.display_name,
        runtime_type: recipe.engine,
        endpoint: `http://${host.ssh_host}:${port}/v1`,
        healthcheck_path: null,
        model_identifier: recipe.model_identifier,
        launch_command: rendered.launch_command,
        stop_command: rendered.stop_command,
        process_name: recipe.process_name,
        // Eine 110-GB-Engine teilt sich die Box mit niemandem.
        exclusive_memory: true,
        host_id: host.id,
      });
      await api.runtimes.start(runtime.id);
      queryClient.invalidateQueries({ queryKey: ["runtimes"] });
      queryClient.invalidateQueries({ queryKey: ["local-registry"] });
      setCreated(true);
    } catch (err) {
      setError(extractApiError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-0 sm:p-4"
      onClick={onClose}
    >
      <div className="absolute inset-0 bg-black/60" />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t("installDialogTitle", { name: recipe.display_name })}
        onClick={(e) => e.stopPropagation()}
        className="relative w-full max-w-xl rounded-t-md sm:rounded-md border border-subtle bg-elevated overflow-hidden max-h-[92dvh] flex flex-col"
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-subtle">
          <div className="min-w-0">
            <div className="label-sys label-sys--dim">{t("installKicker")}</div>
            <h2 className="text-sm font-semibold text-primary truncate">
              {recipe.display_name}
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("close")}
            className="text-muted cursor-pointer"
          >
            <X size={16} />
          </button>
        </div>

        <div className="flex flex-col gap-3 p-5 overflow-y-auto">
          {recipe.notes && (
            <p className="text-[11px] text-muted whitespace-pre-wrap">{recipe.notes}</p>
          )}

          <label className="flex flex-col gap-1">
            <span className="label-sys label-sys--dim">{t("installHostLabel")}</span>
            {sshHosts.length > 0 ? (
              <select
                value={hostId}
                onChange={(e) => setHostId(e.target.value)}
                aria-label={t("installHostLabel")}
                className="rounded-md border border-subtle bg-surface px-2 py-2.5 sm:py-1.5 min-h-11 sm:min-h-0 text-xs text-primary cursor-pointer"
              >
                {sshHosts.map((h) => (
                  <option key={h.id} value={h.id}>
                    {h.display_name}
                  </option>
                ))}
              </select>
            ) : (
              <span data-testid="ssh-deploy-no-hosts" className="text-xs text-warn">
                {t("installNoHosts")}
              </span>
            )}
          </label>

          <div className="grid grid-cols-2 gap-3">
            <label className="flex flex-col gap-1">
              <span className="label-sys label-sys--dim">{t("installSlugLabel")}</span>
              <input
                type="text"
                value={runtimeSlug}
                onChange={(e) => setRuntimeSlug(slugify(e.target.value))}
                aria-label={t("installSlugLabel")}
                className="rounded-md border border-subtle bg-surface px-2 py-2.5 sm:py-1.5 min-h-11 sm:min-h-0 font-mono text-xs text-primary outline-none"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="label-sys label-sys--dim">{t("installPortLabel")}</span>
              <input
                type="number"
                value={port}
                onChange={(e) => setPort(Number(e.target.value))}
                aria-label={t("installPortLabel")}
                className="rounded-md border border-subtle bg-surface px-2 py-2.5 sm:py-1.5 min-h-11 sm:min-h-0 font-mono text-xs text-primary outline-none"
              />
            </label>
          </div>

          {neighbours.length > 0 && (
            <div
              data-testid="ssh-deploy-exclusive-warning"
              className="flex items-start gap-2 rounded-md border border-warn bg-warn-subtle px-2.5 py-2 text-[11px] text-warn"
            >
              <AlertTriangle size={12} className="shrink-0 mt-px" />
              <span>
                {t("installExclusiveWarning", {
                  names: neighbours.map((rt) => rt.display_name).join(", "),
                })}
              </span>
            </div>
          )}

          {error && (
            <div
              data-testid="ssh-deploy-error"
              className="rounded-md border border-err bg-err-subtle px-2.5 py-2 text-[11px] text-err"
            >
              {error}
            </div>
          )}

          {log.length > 0 && (
            <div
              data-testid="ssh-deploy-install-log"
              className="rounded-md border border-subtle bg-surface px-2.5 py-2 max-h-48 overflow-y-auto font-mono text-[10px] flex flex-col gap-0.5"
            >
              {log.map((line, i) => (
                <div
                  key={i}
                  className={`whitespace-pre-wrap break-words ${
                    line.level === "error"
                      ? "text-err"
                      : line.level === "warn"
                        ? "text-warn"
                        : "text-muted"
                  }`}
                >
                  {line.text}
                </div>
              ))}
              <div ref={logEnd} />
            </div>
          )}

          {status === "done" && (
            <div
              data-testid="ssh-deploy-install-done"
              className="flex items-center gap-1.5 text-[11px] text-ok"
            >
              <CheckCircle2 size={12} />
              {t("installDone")}
            </div>
          )}

          {created && (
            <div
              data-testid="ssh-deploy-created"
              className="flex items-center gap-1.5 text-[11px] text-ok"
            >
              <CheckCircle2 size={12} />
              {t("installRuntimeCreated")}
            </div>
          )}
        </div>

        <div className="flex flex-wrap items-center justify-end gap-2 px-5 py-4 border-t border-subtle">
          <button
            type="button"
            data-testid="ssh-deploy-install"
            onClick={startInstall}
            disabled={busy || polling || !hostId}
            className="inline-flex items-center gap-1.5 rounded-md border border-subtle bg-surface px-3 py-2.5 sm:py-1.5 min-h-11 sm:min-h-0 text-xs text-muted cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {polling ? <Loader2 size={11} className="animate-spin" /> : <Download size={11} />}
            {polling ? t("installRunning") : t("installStart")}
          </button>
          <button
            type="button"
            data-testid="ssh-deploy-create"
            onClick={createAndStart}
            disabled={busy || polling || !hostId || !runtimeSlug || created}
            className="inline-flex items-center gap-1.5 rounded-md border border-accent bg-accent-subtle px-3 py-2.5 sm:py-1.5 min-h-11 sm:min-h-0 text-xs text-accent cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {busy ? <Loader2 size={11} className="animate-spin" /> : <Rocket size={11} />}
            {t("installCreateAndStart")}
          </button>
        </div>
      </div>
    </div>
  );
}

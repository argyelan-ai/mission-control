"use client";

/**
 * The Claude account card (Welle 2, PR 2-4; ADR-093 view · test · replace · remove).
 *
 * One vault token signs in every Claude agent. This card shows whether it
 * works (the config-check registry, never run just by opening the page —
 * decision E8a), reconnects through `claude setup-token` in a throwaway
 * container, saves a pasted token through the normal vault endpoints,
 * disconnects, and restarts the agents that still hold the old token
 * through the normal agent restart. No second path for any of these.
 */

import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { ExternalLink, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import type { ClaudeAccountStatus, ClaudeLoginState } from "@/lib/types";
import { C, STATUS_TEXT } from "@/lib/colors";
import { timeAgo } from "@/lib/utils";
import { StatusDot } from "@/components/shared/StatusDot";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";

// Must match routers/secrets.py PROVIDER_TEMPLATES (the one vault key).
const TOKEN_KEY = "claude_code_oauth_token";
const TOKEN_PROVIDER = "anthropic-claude-code";
const STATUS_QUERY = ["claude-account"] as const;

const cardStyle = {
  background: C.bgSurface,
  border: `1px solid ${C.border}`,
  borderRadius: 12,
} as const;

const inputStyle = {
  backgroundColor: C.bgDeep,
  borderWidth: 1,
  borderStyle: "solid",
  borderColor: "var(--color-border)",
  color: "var(--color-text-primary)",
} as const;

// K11: one accent button per screen, the rest are quiet text buttons.
function PrimaryButton(props: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      {...props}
      className="min-h-11 px-4 rounded-lg text-sm font-medium cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed text-[var(--color-on-accent)]"
      style={{ backgroundColor: C.accent }}
    />
  );
}

function QuietButton({ danger, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { danger?: boolean }) {
  return (
    <button
      {...props}
      className="min-h-11 px-3 rounded-lg text-sm cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
      style={{ color: danger ? STATUS_TEXT.error : "var(--color-text-secondary)" }}
    />
  );
}

const LOGIN_ACTIVE: ClaudeLoginState["state"][] = ["starting", "waiting_for_code", "verifying", "failed"];

// ── Reconnect panel ─────────────────────────────────────────────────────────

function LoginPanel({
  login,
  onChange,
  onDone,
}: {
  login: ClaudeLoginState;
  onChange: (next: ClaudeLoginState) => void;
  onDone: () => void;
}) {
  const t = useTranslations("settings.claude.login");
  const [code, setCode] = useState("");

  // The link appears a few seconds after start; ask until it does.
  const { data: polled } = useQuery<ClaudeLoginState>({
    queryKey: ["claude-login"],
    queryFn: () => api.claudeAccount.loginStatus(),
    enabled: login.state === "starting",
    refetchInterval: 1500,
    retry: false,
  });
  useEffect(() => {
    if (polled && login.state === "starting" && polled.state !== "starting") onChange(polled);
  }, [polled, login.state, onChange]);

  const submit = useMutation({
    mutationFn: (value: string) => api.claudeAccount.submitCode(value),
    onMutate: () => onChange({ ...login, state: "verifying" }),
    onSuccess: (next) => onChange(next),
    onError: () => onChange({ state: "failed", url: null, error: t("failed") }),
  });

  const restart = useMutation({
    mutationFn: () => api.claudeAccount.startLogin(),
    onSuccess: (next) => onChange(next),
    onError: () => onChange({ state: "failed", url: null, error: t("failed") }),
  });

  const cancel = async () => {
    try {
      await api.claudeAccount.cancelLogin();
    } catch {
      // The container ends on its own after 15 minutes; nothing to keep.
    }
    onDone();
  };

  if (login.state === "starting" || login.state === "verifying") {
    return (
      <div className="flex items-center gap-2 text-sm" style={{ color: "var(--color-text-secondary)" }}>
        <Loader2 size={14} className="animate-spin" />
        <span>{login.state === "starting" ? t("starting") : t("verifying")}</span>
        {login.state === "starting" && (
          <QuietButton onClick={cancel} type="button">
            {t("cancel")}
          </QuietButton>
        )}
      </div>
    );
  }

  if (login.state === "failed") {
    return (
      <div className="space-y-2">
        <p className="text-sm" style={{ color: STATUS_TEXT.error }} data-testid="claude-login-error">
          {login.error || t("failed")}
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <QuietButton onClick={() => restart.mutate()} disabled={restart.isPending} type="button">
            {t("startAgain")}
          </QuietButton>
          <QuietButton onClick={cancel} type="button">
            {t("cancel")}
          </QuietButton>
        </div>
      </div>
    );
  }

  // waiting_for_code
  return (
    <ol className="space-y-4 text-sm" style={{ color: "var(--color-text-secondary)" }}>
      <li className="space-y-1">
        {login.url && (
          <a
            href={login.url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-2 min-h-11 font-medium"
            style={{ color: C.accent }}
          >
            {t("step1")}
            <ExternalLink size={14} />
          </a>
        )}
        <p className="text-xs" style={{ color: "var(--color-text-muted)", maxWidth: "72ch" }}>
          {t("step1Hint")}
        </p>
      </li>
      <li>
        <label htmlFor="claude-login-code" className="block text-sm mb-2" style={{ color: "var(--color-text-primary)" }}>
          {t("codeLabel")}
        </label>
        <form
          className="flex flex-wrap gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (code.trim()) submit.mutate(code.trim());
          }}
        >
          <input
            id="claude-login-code"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder={t("codePlaceholder")}
            autoComplete="off"
            spellCheck={false}
            className="flex-1 min-w-0 min-h-11 rounded-lg px-3 text-sm outline-none font-mono"
            style={inputStyle}
          />
          <PrimaryButton type="submit" disabled={!code.trim() || submit.isPending}>
            {t("finish")}
          </PrimaryButton>
          <QuietButton onClick={cancel} type="button">
            {t("cancel")}
          </QuietButton>
        </form>
      </li>
    </ol>
  );
}

// ── Paste a token (the existing vault path) ─────────────────────────────────

function PasteToken({ configured, onSaved }: { configured: boolean; onSaved: () => void }) {
  const t = useTranslations("settings.claude.paste");
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");

  const save = useMutation({
    mutationFn: async (token: string) => {
      if (configured) await api.secrets.update(TOKEN_KEY, { value: token });
      else await api.secrets.create({ key: TOKEN_KEY, value: token, provider: TOKEN_PROVIDER });
      // ADR-093 §2: test on save. The one moment besides a click that may
      // spend the plan's quota (decision E8a).
      return api.configChecks.run("secret", TOKEN_KEY);
    },
    onSuccess: () => {
      setValue("");
      setOpen(false);
      notify.success(t("saved"));
      onSaved();
    },
    onError: () => notify.error(t("saveFailed")),
  });

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="min-h-11 text-sm cursor-pointer"
        style={{ color: "var(--color-text-secondary)" }}
      >
        {t("toggle")}
      </button>
    );
  }

  return (
    <div className="space-y-2">
      <label htmlFor="claude-paste-token" className="block text-sm" style={{ color: "var(--color-text-primary)" }}>
        {t("label")}
      </label>
      <form
        className="flex flex-wrap gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (value.trim()) save.mutate(value.trim());
        }}
      >
        <input
          id="claude-paste-token"
          type="password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="sk-ant-oat01-..."
          autoComplete="off"
          spellCheck={false}
          className="flex-1 min-w-0 min-h-11 rounded-lg px-3 text-sm outline-none font-mono"
          style={inputStyle}
        />
        <QuietButton type="submit" disabled={!value.trim() || save.isPending}>
          {save.isPending ? <Loader2 size={14} className="animate-spin" /> : t("save")}
        </QuietButton>
      </form>
      <p className="text-xs" style={{ color: "var(--color-text-muted)", maxWidth: "72ch" }}>
        {t("hint")}
      </p>
    </div>
  );
}

// ── Agents that still hold the old token ────────────────────────────────────

function StaleAgents({ status, onRestarted }: { status: ClaudeAccountStatus; onRestarted: () => void }) {
  const t = useTranslations("settings.claude");
  const [confirm, setConfirm] = useState(false);
  const idle = status.stale_agents.filter((a) => !a.busy);
  const anyBusy = idle.length < status.stale_agents.length;

  const restart = useMutation({
    mutationFn: async () => {
      let done = 0;
      for (const agent of idle) {
        try {
          await api.agents.restartContainer(agent.id);
          done += 1;
        } catch {
          notify.error(t("restartFailed", { name: agent.name }));
        }
      }
      return done;
    },
    onSuccess: (done) => {
      setConfirm(false);
      if (done) notify.success(t("restarted", { count: done }));
      onRestarted();
    },
  });

  return (
    <div className="mc-card p-4 space-y-3" style={cardStyle} data-testid="claude-stale" data-region="claude-stale">
      <div className="flex items-center gap-2">
        <StatusDot status="warning" />
        <span className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
          {t("staleTitle", { count: status.stale_agents.length })}
        </span>
      </div>
      <p className="text-sm" style={{ color: "var(--color-text-secondary)", maxWidth: "72ch" }}>
        {t("staleBody")}
      </p>
      <ul className="space-y-1 text-sm">
        {status.stale_agents.map((agent) => (
          <li key={agent.id} className="flex items-center gap-2">
            <span style={{ color: "var(--color-text-primary)" }}>{agent.name}</span>
            {agent.busy && (
              <span className="text-xs" style={{ color: "var(--color-text-muted)" }}>
                · {t("staleBusy")}
              </span>
            )}
          </li>
        ))}
      </ul>
      {anyBusy && (
        <p className="text-xs" style={{ color: "var(--color-text-muted)" }}>
          {t("staleBusyNote")}
        </p>
      )}
      {idle.length > 0 && (
        <QuietButton type="button" onClick={() => setConfirm(true)}>
          {t("restartAgents", { count: idle.length })}
        </QuietButton>
      )}
      <ConfirmDialog
        open={confirm}
        kicker={t("title")}
        title={t("restartTitle")}
        body={t("restartBody", { count: idle.length })}
        confirmLabel={t("restartConfirm")}
        danger={false}
        loading={restart.isPending}
        onConfirm={() => restart.mutate()}
        onCancel={() => setConfirm(false)}
      />
    </div>
  );
}

// ── Tab ─────────────────────────────────────────────────────────────────────

export function ClaudeAccountTab() {
  const t = useTranslations("settings.claude");
  const tLogin = useTranslations("settings.claude.login");
  const locale = useLocale();
  const queryClient = useQueryClient();
  const [login, setLogin] = useState<ClaudeLoginState | null>(null);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);

  const { data: status, isLoading } = useQuery<ClaudeAccountStatus>({
    queryKey: STATUS_QUERY,
    queryFn: () => api.claudeAccount.status(),
    // Reading is free (no call to Claude); keep it calm all the same.
    refetchOnWindowFocus: false,
    retry: false,
  });

  const refresh = () => queryClient.invalidateQueries({ queryKey: STATUS_QUERY });

  // A login started on another device (or before a reload) is picked up.
  const activeLogin =
    login ?? (status && LOGIN_ACTIVE.includes(status.login.state) ? status.login : null);

  useEffect(() => {
    if (activeLogin?.state !== "connected") return;
    if (activeLogin.error) notify.warning(tLogin("savedNotWorking"));
    else notify.success(tLogin("connected"));
    api.claudeAccount.cancelLogin().catch(() => undefined);
    setLogin({ state: "idle", url: null, error: null });
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeLogin?.state]);

  const startLogin = useMutation({
    mutationFn: () => api.claudeAccount.startLogin(),
    onMutate: () => setLogin({ state: "starting", url: null, error: null }),
    onSuccess: (next) => setLogin(next),
    onError: () => setLogin({ state: "failed", url: null, error: tLogin("failed") }),
  });

  const test = useMutation({
    mutationFn: () => api.configChecks.run("secret", TOKEN_KEY),
    onSuccess: ({ item }) => {
      if (item.state === "ok") notify.success(t("testOk"));
      else notify.error(t("testFailed"));
      refresh();
    },
    onError: () => notify.error(t("testFailed")),
  });

  const disconnect = useMutation({
    mutationFn: () => api.secrets.delete(TOKEN_KEY),
    onSuccess: () => {
      setConfirmDisconnect(false);
      notify.success(t("disconnected"));
      queryClient.invalidateQueries({ queryKey: ["secrets"] });
      refresh();
    },
    onError: () => notify.error(t("disconnectFailed")),
  });

  const check = status?.check ?? null;
  const configured = !!status?.configured;
  const state = !configured ? "none" : check?.state ?? "never_tested";
  const dot = state === "ok" ? "online" : state === "fail" ? "error" : state === "none" ? "idle" : "warning";
  const headline =
    state === "none"
      ? t("statusNotConnected")
      : state === "ok"
      ? t("statusOk", { ago: timeAgo(check?.checked_at ?? null, locale) })
      : state === "fail"
      ? t("statusFail")
      : t("statusNeverTested");
  const loginOpen = !!activeLogin && LOGIN_ACTIVE.includes(activeLogin.state);

  return (
    <motion.div
      key="claude"
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -4 }}
      transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
    >
      <div className="mb-6">
        <h2 className="text-base font-semibold" style={{ color: "var(--color-text-primary)" }}>
          {t("title")}
        </h2>
        <p className="text-sm mt-1" style={{ color: "var(--color-text-muted)", maxWidth: "72ch" }}>
          {t("intro")}
        </p>
      </div>

      {isLoading || !status ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="animate-spin" size={20} style={{ color: "var(--color-text-muted)" }} />
        </div>
      ) : (
        <div className="space-y-4">
          <div className="mc-card p-4 space-y-3" style={cardStyle} data-region="claude-status">
            <div className="flex items-center gap-2">
              <StatusDot status={dot} size="lg" />
              <span
                className="text-sm font-medium"
                style={{ color: "var(--color-text-primary)" }}
                data-testid="claude-status-headline"
              >
                {headline}
              </span>
              {test.isPending && (
                <Loader2 size={12} className="animate-spin" style={{ color: "var(--color-text-muted)" }} />
              )}
            </div>

            {state === "fail" && check?.message && (
              <p className="text-sm" style={{ color: STATUS_TEXT.error }} data-testid="claude-check-message">
                {check.message}
              </p>
            )}
            <p className="text-xs" style={{ color: "var(--color-text-muted)", maxWidth: "72ch" }}>
              {configured
                ? t("tokenSaved", { ago: timeAgo(status.token_updated_at, locale) })
                : t("notConnectedHint")}
            </p>

            {loginOpen && activeLogin ? (
              <div className="pt-2">
                <LoginPanel
                  login={activeLogin}
                  onChange={setLogin}
                  onDone={() => setLogin({ state: "idle", url: null, error: null })}
                />
              </div>
            ) : (
              <>
                <div className="flex flex-wrap items-center gap-2">
                  {!configured ? (
                    <PrimaryButton type="button" onClick={() => startLogin.mutate()}>
                      {t("connect")}
                    </PrimaryButton>
                  ) : state === "fail" ? (
                    <PrimaryButton type="button" onClick={() => startLogin.mutate()}>
                      {t("reconnect")}
                    </PrimaryButton>
                  ) : null}
                  {configured && (
                    <QuietButton type="button" onClick={() => test.mutate()} disabled={test.isPending}>
                      {test.isPending ? t("testing") : t("testNow")}
                    </QuietButton>
                  )}
                  {configured && state !== "fail" && (
                    <QuietButton type="button" onClick={() => startLogin.mutate()}>
                      {t("reconnect")}
                    </QuietButton>
                  )}
                  {configured && (
                    <QuietButton type="button" danger onClick={() => setConfirmDisconnect(true)}>
                      {t("disconnect")}
                    </QuietButton>
                  )}
                </div>
                {configured && (
                  <p className="text-xs" style={{ color: "var(--color-text-muted)", maxWidth: "72ch" }}>
                    {t("testCostHint")}
                  </p>
                )}
                <PasteToken configured={configured} onSaved={refresh} />
              </>
            )}
          </div>

          {status.stale_agents.length > 0 && <StaleAgents status={status} onRestarted={refresh} />}

          {status.host_agents > 0 && (
            <p className="text-xs" style={{ color: "var(--color-text-muted)", maxWidth: "72ch" }} data-testid="claude-host-note">
              {t("hostNote", { count: status.host_agents })}
            </p>
          )}
        </div>
      )}

      <ConfirmDialog
        open={confirmDisconnect}
        kicker={t("title")}
        title={t("disconnectTitle")}
        body={t("disconnectBody")}
        confirmLabel={t("disconnect")}
        loading={disconnect.isPending}
        onConfirm={() => disconnect.mutate()}
        onCancel={() => setConfirmDisconnect(false)}
      />
    </motion.div>
  );
}

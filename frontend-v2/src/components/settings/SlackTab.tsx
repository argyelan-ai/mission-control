"use client";

import { useState } from "react";
import { motion } from "framer-motion";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronRight, Copy, Eye, EyeOff, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import type { SecretEntry, SlackConnectionResult } from "@/lib/types";
import { C, STATUS_TEXT } from "@/lib/colors";
import { StatusDot } from "@/components/shared/StatusDot";

// The two fixed fields. Key names match the backend secret catalog
// (routers/secrets.py) so an operator finds them instead of inventing them.
const BOT_TOKEN_KEY = "slack_bot_token";
const APP_TOKEN_KEY = "slack_app_token";

const BOT_SCOPES = [
  { scope: "chat:write", purpose: "post messages" },
  { scope: "chat:write.customize", purpose: "post under each agent's own name and avatar" },
  { scope: "channels:read", purpose: "see the channels in the workspace" },
  { scope: "channels:manage", purpose: "create a channel per project" },
  { scope: "channels:history", purpose: "read the conversation MC is part of" },
  { scope: "app_mentions:read", purpose: "notice when an agent is mentioned" },
  { scope: "im:history", purpose: "read direct messages to the app" },
  { scope: "im:write", purpose: "reply in direct messages" },
  { scope: "users:read", purpose: "resolve who wrote a message" },
  { scope: "reactions:write", purpose: "acknowledge a task with an emoji" },
  { scope: "files:write", purpose: "upload logs and screenshots" },
];

const SCOPE_LIST = BOT_SCOPES.map((s) => s.scope).join(", ");

const EVENTS = ["message.channels", "app_mention", "message.im"];

const cardStyle = {
  background: C.bgSurface,
  border: `1px solid ${C.border}`,
  borderRadius: 12,
} as const;

// ── Small building blocks (same vocabulary as the other settings sections) ────

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <span
      className="text-xs font-medium uppercase tracking-widest block mb-1.5"
      style={{ color: "var(--color-text-secondary)" }}
    >
      {children}
    </span>
  );
}

function TokenField({
  label,
  secretKey,
  placeholder,
  hint,
  existing,
  onSave,
  saving,
}: {
  label: string;
  secretKey: string;
  placeholder: string;
  hint: string;
  existing: SecretEntry | undefined;
  onSave: (value: string) => void;
  saving: boolean;
}) {
  const [value, setValue] = useState("");
  const [reveal, setReveal] = useState(false);
  const inputId = `slack-${secretKey}`;

  return (
    <div>
      <label htmlFor={inputId}>
        <FieldLabel>{label}</FieldLabel>
      </label>
      <div className="flex gap-2">
        <div className="relative flex-1">
          <input
            id={inputId}
            type={reveal ? "text" : "password"}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={existing ? `${existing.value_masked} (paste to replace)` : placeholder}
            aria-label={label}
            autoComplete="off"
            spellCheck={false}
            className="w-full rounded-lg px-3 py-2.5 pr-10 text-sm outline-none transition-all duration-200 font-mono"
            style={{
              backgroundColor: C.bgDeep,
              borderWidth: 1,
              borderStyle: "solid",
              borderColor: "var(--color-border)",
              color: "var(--color-text-primary)",
            }}
            onFocus={(e) => { e.currentTarget.style.borderColor = C.borderAccent; }}
            onBlur={(e) => { e.currentTarget.style.borderColor = "var(--color-border)"; }}
          />
          <button
            type="button"
            onClick={() => setReveal((r) => !r)}
            aria-label={reveal ? `Hide ${label}` : `Show ${label}`}
            className="absolute right-3 top-1/2 -translate-y-1/2 cursor-pointer"
            style={{ color: "var(--color-text-muted)" }}
          >
            {reveal ? <EyeOff size={14} /> : <Eye size={14} />}
          </button>
        </div>
        <button
          onClick={() => { onSave(value.trim()); setValue(""); setReveal(false); }}
          disabled={!value.trim() || saving}
          className="shrink-0 px-3 py-2 rounded-lg text-xs font-medium cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed text-[var(--color-on-accent)]"
          style={{ backgroundColor: C.accent }}
        >
          {saving ? <Loader2 size={12} className="animate-spin" /> : "Save"}
        </button>
      </div>
      <p className="text-xs mt-1.5" style={{ color: "var(--color-text-muted)" }}>
        {hint}
      </p>
    </div>
  );
}

// ── Setup guide (disclosure) ──────────────────────────────────────────────────

function SetupGuide() {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState<"scopes" | "events" | null>(null);

  async function copy(what: "scopes" | "events", text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(what);
      setTimeout(() => setCopied(null), 1600);
    } catch {
      notify.error("Could not access the clipboard. Select the text and copy it manually.");
    }
  }

  return (
    <div className="mc-card" style={cardStyle}>
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls="slack-setup-guide"
        className="flex items-center gap-2 w-full p-4 text-left cursor-pointer"
      >
        <ChevronRight
          size={14}
          style={{
            color: "var(--color-text-muted)",
            transform: open ? "rotate(90deg)" : "none",
            transition: "transform 180ms cubic-bezier(0.16,1,0.3,1)",
          }}
        />
        <span className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
          Set up the Slack app
        </span>
        <span className="text-xs ml-auto" style={{ color: "var(--color-text-muted)" }}>
          {open ? "Hide" : "Show"} the seven steps
        </span>
      </button>

      {open && (
        <div
          id="slack-setup-guide"
          className="px-4 pb-4 pt-1 space-y-4 text-sm"
          style={{ color: "var(--color-text-secondary)", maxWidth: "72ch" }}
        >
          <p>
            MC runs on your own machine and has no public URL, so Slack cannot call
            it. MC opens the connection instead, over{" "}
            <span style={{ color: "var(--color-text-primary)" }}>Socket Mode</span>.
            That is why there is no Request URL to fill in anywhere, and why the
            second token exists.
          </p>

          <ol className="space-y-3">
            <GuideStep n={1} title="Create the app">
              Go to{" "}
              <a
                href="https://api.slack.com/apps"
                target="_blank"
                rel="noreferrer"
                className="underline"
                style={{ color: C.accent }}
              >
                api.slack.com/apps
              </a>
              , choose <em>Create New App</em>, then <em>From scratch</em>. Name it
              (for example Mission Control) and pick your workspace.
            </GuideStep>

            <GuideStep n={2} title="Turn on Socket Mode">
              Open <em>Socket Mode</em> in the sidebar and enable it.
            </GuideStep>

            <GuideStep n={3} title="Create the app-level token">
              It lives under <em>Basic Information → App-Level Tokens</em>, not on
              the Socket Mode page. Generate a token with the{" "}
              <code style={codeStyle}>connections:write</code> scope. It starts with{" "}
              <code style={codeStyle}>xapp-</code>.
            </GuideStep>

            <GuideStep n={4} title="Add the bot token scopes">
              Under <em>OAuth &amp; Permissions → Bot Token Scopes</em>, add all
              eleven scopes. Copy them instead of typing them:
              <div
                className="mt-2 rounded-lg p-3 flex items-start gap-3"
                style={{ backgroundColor: C.bgDeep, border: `1px solid ${C.border}` }}
              >
                <code
                  className="text-xs font-mono flex-1 break-words"
                  style={{ color: "var(--color-text-primary)" }}
                  data-testid="slack-scope-list"
                >
                  {SCOPE_LIST}
                </code>
                <button
                  onClick={() => copy("scopes", SCOPE_LIST)}
                  aria-label="Copy scope list"
                  className="shrink-0 flex items-center gap-1.5 px-2 py-1 rounded text-xs cursor-pointer"
                  style={{ backgroundColor: C.accentSubtle, color: C.accent }}
                >
                  {copied === "scopes" ? <Check size={12} /> : <Copy size={12} />}
                  {copied === "scopes" ? "Copied" : "Copy"}
                </button>
              </div>
              <ul className="mt-2 space-y-1">
                {BOT_SCOPES.map((s) => (
                  <li key={s.scope} className="text-xs flex gap-2">
                    <code style={{ ...codeStyle, minWidth: 150 }}>{s.scope}</code>
                    <span style={{ color: "var(--color-text-muted)" }}>{s.purpose}</span>
                  </li>
                ))}
              </ul>
              <p className="text-xs mt-2" style={{ color: STATUS_TEXT.warning }}>
                Without <code style={codeStyle}>chat:write.customize</code> every agent
                posts under the same app name, so the team chat reads like one voice
                instead of your fleet. It is easy to miss because everything else
                still works.
              </p>
            </GuideStep>

            <GuideStep n={5} title="Subscribe to events">
              Under <em>Event Subscriptions</em>, enable events and add these bot
              events. No Request URL is asked for once Socket Mode is on.
              <div
                className="mt-2 rounded-lg p-3 flex items-start gap-3"
                style={{ backgroundColor: C.bgDeep, border: `1px solid ${C.border}` }}
              >
                <code className="text-xs font-mono flex-1" style={{ color: "var(--color-text-primary)" }}>
                  {EVENTS.join(", ")}
                </code>
                <button
                  onClick={() => copy("events", EVENTS.join(", "))}
                  aria-label="Copy event list"
                  className="shrink-0 flex items-center gap-1.5 px-2 py-1 rounded text-xs cursor-pointer"
                  style={{ backgroundColor: C.accentSubtle, color: C.accent }}
                >
                  {copied === "events" ? <Check size={12} /> : <Copy size={12} />}
                  {copied === "events" ? "Copied" : "Copy"}
                </button>
              </div>
            </GuideStep>

            <GuideStep n={6} title="Install the app">
              Back under <em>OAuth &amp; Permissions</em>, install the app to the
              workspace. Copy the <em>Bot User OAuth Token</em>, it starts with{" "}
              <code style={codeStyle}>xoxb-</code>.
            </GuideStep>

            <GuideStep n={7} title="Paste both tokens above">
              Bot token in the first field, app-level token in the second, then run
              Test connection. Finally invite the bot into a channel with{" "}
              <code style={codeStyle}>/invite @your-app-name</code>, otherwise it
              cannot read or post there.
            </GuideStep>
          </ol>

          <p className="text-xs" style={{ color: "var(--color-text-muted)" }}>
            The same guide, including a troubleshooting list, is in the repo under{" "}
            <code style={codeStyle}>docs/setup/slack.md</code>.
          </p>
        </div>
      )}
    </div>
  );
}

const codeStyle: React.CSSProperties = {
  fontFamily: "var(--font-mono, ui-monospace), monospace",
  fontSize: "11px",
  padding: "1px 4px",
  borderRadius: 3,
  backgroundColor: C.bgElevated,
  color: "var(--color-text-primary)",
};

function GuideStep({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <li className="flex gap-3">
      <span
        className="shrink-0 w-5 h-5 rounded flex items-center justify-center text-[11px] font-mono mt-0.5"
        style={{ backgroundColor: C.bgElevated, color: "var(--color-text-secondary)" }}
      >
        {n}
      </span>
      <div className="flex-1 min-w-0">
        <div className="font-medium" style={{ color: "var(--color-text-primary)" }}>
          {title}
        </div>
        <div className="mt-0.5">{children}</div>
      </div>
    </li>
  );
}

// ── Tab ───────────────────────────────────────────────────────────────────────

export function SlackTab() {
  const queryClient = useQueryClient();
  const [savingKey, setSavingKey] = useState<string | null>(null);

  const { data: secrets } = useQuery<SecretEntry[]>({
    queryKey: ["secrets"],
    queryFn: () => api.secrets.list(),
  });

  const {
    data: status,
    isFetching: testing,
    refetch,
  } = useQuery<SlackConnectionResult>({
    queryKey: ["slack-connection"],
    queryFn: () => api.slack.testConnection(),
    // The status is the point of this page, so it runs once on open. It stays
    // put afterwards: no window-focus refetch hammering the Slack API.
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const saveMutation = useMutation({
    mutationFn: async ({ key, value }: { key: string; value: string }) => {
      const template = { provider: "slack" };
      const exists = (secrets ?? []).some((s) => s.key === key);
      return exists
        ? api.secrets.update(key, { value })
        : api.secrets.create({ key, value, ...template });
    },
    onMutate: ({ key }) => setSavingKey(key),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["secrets"] });
      notify.success("Token saved");
      await refetch();
    },
    onError: () => notify.error("Could not save the token"),
    onSettled: () => setSavingKey(null),
  });

  const byKey = new Map((secrets ?? []).map((s) => [s.key, s]));
  const botSecret = byKey.get(BOT_TOKEN_KEY);
  const appSecret = byKey.get(APP_TOKEN_KEY);

  const nothingSet = !botSecret && !appSecret;
  const dot: "online" | "warning" | "error" | "idle" = nothingSet
    ? "idle"
    : status?.connected
    ? status.socket_mode_ready
      ? "online"
      : "warning"
    : "error";

  const headline = nothingSet
    ? "Not set up"
    : status?.connected
    ? status.socket_mode_ready
      ? `Connected to ${status.team ?? "Slack"}`
      : `Reachable in ${status.team ?? "Slack"}, Socket Mode not ready`
    : status
    ? "Not connected"
    : "Checking…";

  return (
    <motion.div
      key="slack"
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -4 }}
      transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
    >
      <div className="mb-6">
        <h2 className="text-base font-semibold" style={{ color: "var(--color-text-primary)" }}>
          Slack
        </h2>
        <p className="text-sm mt-1" style={{ color: "var(--color-text-muted)", maxWidth: "72ch" }}>
          Run the team chat with your agents in Slack. MC connects outwards over Socket
          Mode, so it works from a self-hosted machine without a public URL.
        </p>
      </div>

      <div className="space-y-4">
        {/* Status — the one thing that must be readable at a glance */}
        <div className="mc-card p-4 space-y-2.5" style={cardStyle}>
          <div className="flex items-center gap-2.5">
            <StatusDot status={dot} size="lg" />
            <span
              className="text-sm font-medium"
              style={{ color: "var(--color-text-primary)" }}
              data-testid="slack-status-headline"
            >
              {headline}
            </span>
            {testing && (
              <Loader2 size={12} className="animate-spin" style={{ color: "var(--color-text-muted)" }} />
            )}
          </div>

          {status?.connected && status.bot_user && (
            <div className="flex items-center gap-2 text-xs">
              <span style={{ color: "var(--color-text-muted)" }}>Bot</span>
              <span className="font-mono" style={{ color: "var(--color-text-primary)" }}>
                {status.bot_user}
              </span>
            </div>
          )}

          <div className="flex items-center gap-2 text-xs">
            <span style={{ color: "var(--color-text-muted)" }}>Socket Mode</span>
            <span style={{ color: status?.socket_mode_ready ? C.online : "var(--color-text-secondary)" }}>
              {status?.socket_mode_ready ? "ready" : "not ready"}
            </span>
          </div>

          {status?.error && (
            <p className="text-xs pt-1" style={{ color: STATUS_TEXT.error }} data-testid="slack-error">
              {status.error}
            </p>
          )}
          {status?.app_token_error && (
            <p className="text-xs" style={{ color: STATUS_TEXT.warning }} data-testid="slack-app-token-error">
              {status.app_token_error}
            </p>
          )}

          <button
            onClick={() => refetch()}
            disabled={testing}
            className="mt-1 text-xs px-2.5 py-1.5 rounded-lg cursor-pointer disabled:opacity-50 transition-all"
            style={{ background: "var(--color-bg-elevated)", color: "var(--color-text-secondary)" }}
          >
            {testing ? "Testing…" : "Test connection"}
          </button>
        </div>

        {/* Tokens */}
        <div className="mc-card p-4 space-y-4" style={cardStyle}>
          <TokenField
            label="Bot User OAuth Token"
            secretKey={BOT_TOKEN_KEY}
            placeholder="xoxb-..."
            hint="OAuth & Permissions → Bot User OAuth Token. Lets agents read and post."
            existing={botSecret}
            saving={savingKey === BOT_TOKEN_KEY}
            onSave={(value) => saveMutation.mutate({ key: BOT_TOKEN_KEY, value })}
          />
          <TokenField
            label="App-Level Token"
            secretKey={APP_TOKEN_KEY}
            placeholder="xapp-..."
            hint="Basic Information → App-Level Tokens, scope connections:write. Opens the Socket Mode connection."
            existing={appSecret}
            saving={savingKey === APP_TOKEN_KEY}
            onSave={(value) => saveMutation.mutate({ key: APP_TOKEN_KEY, value })}
          />
        </div>

        <SetupGuide />
      </div>
    </motion.div>
  );
}

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://mc:password@localhost:5432/mission_control"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Auth
    auth_mode: str = "local"
    local_auth_token: str = "dev-token"

    # Phase 29 (ADR-039): gateway_url, openclaw_ws_url, openclaw_token removed.
    # MC no longer dials an OpenClaw Gateway. OPENCLAW_WS_URL / OPENCLAW_TOKEN
    # in `.env` are now ignored (BaseSettings `extra="ignore"` keeps them inert).

    # Budget warnings (cost_collector) — thresholds on model_usage_events.
    # cost_usd is a list-price equivalent (subscription plans don't bill per
    # token); the warnings catch runaway consumption, not an invoice.
    # Env: BUDGET_DAILY_WARNING_TOKENS / BUDGET_MONTHLY_WARNING_USD.
    budget_daily_warning_tokens: int = 400_000_000
    budget_monthly_warning_usd: float = 10_000.0

    # JWT Auth
    jwt_secret_key: str = "change-me-in-production"
    jwt_access_token_expire_minutes: int = 480  # 8 hours

    # Discord
    discord_webhook_ops: str = ""
    discord_bot_token: str = ""
    # Phase 29 (ADR-039) — Discord guild + category previously lived on the
    # legacy `gateways` DB row. The new routers/discord.py (D-04) reads them
    # from env directly. The operator copies the live values from the gateway
    # row into .env (DISCORD_GUILD_ID=... / DISCORD_CATEGORY_ID=...). Defaults
    # are empty so D-14 (backend boots without OpenClaw env vars) stays
    # true even before the .env is updated.
    discord_guild_id: str = ""
    discord_category_id: str = ""

    # Telegram Bot (direct API for approval buttons — operator command chat)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Telegram Reports Bot (separate — agent deliverables, no approval flow)
    # Second bot + chat so info delivery doesn't clutter the command chat.
    telegram_reports_bot_token: str = ""
    telegram_reports_chat_id: str = ""

    # ── Jarvis Telegram-Inbound (ADR-061) ────────────────────────────────
    # Feature-Gate fuer den mobilen Jarvis: Text- + Sprachnachrichten aus dem
    # Operator-Chat werden vom JarvisBrain (OpenAI Function-Calling) beantwortet.
    # Default false → Verhalten exakt wie heute (nur Approval-URL-Buttons).
    # Aktiv nur wenn JARVIS_TELEGRAM_ENABLED=true UND openai_api_key gesetzt ist.
    jarvis_telegram_enabled: bool = False
    # OpenAI-Key fuer den Text-Brain + Sprachnachrichten-Transkription.
    openai_api_key: str = ""
    # Chat-Modell fuer den Text-Brain (konservativer Default, existiert sicher).
    jarvis_text_model: str = "gpt-4o-mini"
    # Transcription-Modell fuer Telegram-Sprachnotizen (ogg/opus direkt).
    jarvis_stt_model: str = "gpt-4o-mini-transcribe"
    # Jarvis-Agent PBKDF2-Token — der Brain fuehrt Tool-Calls ueber den
    # agent-scoped API-Pfad aus (kein Auth-Bypass), derselbe Token wie der
    # voice_worker. Leer → Inbound bleibt inaktiv.
    jarvis_agent_token: str = ""
    # Interne Backend-URL, die der mc_client fuer Self-Calls nutzt (der Brain
    # laeuft IM Backend-Container). Standard: localhost:8000.
    mc_backend_url: str = "http://localhost:8000"

    # ── Jarvis Intelligence (ADR-062) ────────────────────────────────────
    # Frontier-Modell fuer ask_frontier + das Morning-Briefing (schwere
    # Analyse/Planung). Leer → jarvis_core.frontier.DEFAULT_FRONTIER_MODEL.
    jarvis_frontier_model: str = ""
    # Eigenes Gate fuer das ask_frontier-TOOL (Default off — jeder Aufruf kostet
    # pro Anfrage). Off → Tool wird in keinem Kanal-Schema angeboten. Das
    # Morning-Briefing (jarvis_briefing_enabled) ist davon unabhaengig.
    jarvis_frontier_enabled: bool = False
    # Taegliches, LLM-generiertes Morgenbriefing als Vault-Note. Default off;
    # aktiv nur bei JARVIS_BRIEFING_ENABLED=true UND OPENAI_API_KEY gesetzt.
    jarvis_briefing_enabled: bool = False
    # Uhrzeit (Europe/Zurich, "HH:MM") zu der das Briefing generiert wird.
    jarvis_briefing_hour: str = "06:30"

    # MC Base URL (externally reachable, for Telegram URL buttons)
    mc_base_url: str = "http://localhost"

    # Operator display name — how agents address the human behind MC.
    # Rendered into SOUL.md/USER.md templates. Set OPERATOR_NAME in .env.
    operator_name: str = "Operator"

    # Public brand/site name used in generated newsletter copy
    # (header, subject, footer). Set NEWSLETTER_BRAND in .env.
    newsletter_brand: str = "AI Weekly"

    # MC home root — the host's $HOME. The backend container sets HOME_HOST to
    # the host $HOME and bind-mounts ${HOME}/.mc:${HOME}/.mc 1:1, so
    # MC_HOME = home_host/.mc resolves identically in container and on the host.
    # Default = the running user's home → portable / self-hostable (no
    # hardcoded host path). A deployer sets HOME_HOST only when the container
    # HOME differs from the host HOME. pydantic-settings reads the HOME_HOST env var.
    home_host: str = str(Path.home())

    # macOS host login used for SSH-into-host / launchctl GUI-domain calls
    # (Boss-Host bridge, host-agent lifecycle). Empty = derive from the
    # HOME_HOST basename (macOS convention: /Users/<login> is owned by that
    # login). Override via HOST_SSH_USER if the SSH login differs.
    host_ssh_user: str = ""

    # Externally reachable host (Tailscale IP / LAN / DNS) for phone-test deep
    # links + additional CORS origins. Replaces hardcoded IPs in product code.
    # pydantic-settings reads PUBLIC_HOST / EXTRA_CORS_ORIGINS env vars.
    public_host: str = ""
    extra_cors_origins: str = ""  # comma-separated list of additional origins

    # Secrets encryption (Fernet key for MC-managed secrets)
    secrets_encryption_key: str = ""

    # Subagent dispatch (chat_send_isolated instead of chat_send for workers)
    # Kill-switch: USE_SUBAGENT_DISPATCH=false in .env → immediate legacy mode
    use_subagent_dispatch: bool = True

    # Reflection requirement before task completion (Boss autonomy overhaul 2026-04-11)
    # True = last own comment before status=review/done must be comment_type=reflection
    # Enabled in Phase E (2026-04-12) after the worker SOUL audit
    enforce_reflection: bool = True

    # Memory-System / Embeddings (Phase 3, 2026-04-11)
    # Primary: Spark DGX LM Studio OpenAI-compat endpoint
    spark_embedding_url: str = "http://192.0.2.10:1234/v1/embeddings"
    spark_embedding_model: str = "text-embedding-nomic-embed-text-v1.5"
    spark_embedding_timeout: float = 15.0
    # Spark DGX vLLM — LLM completion endpoint
    spark_llm_url: str = "http://192.0.2.10:8000/v1"
    # NOTE: spark_llm_model is a fallback only. The authoritative model
    # identifier is the ``model_identifier`` column on the matching ``runtimes``
    # row, resolved at call time via ``services.runtime_model_resolver``.
    # Callers (spark_client, news_ai_worker) auto-detect recipe swaps via
    # the resolver and fall back to this value only if the resolver fails.
    spark_llm_model: str = "Qwen/Qwen3.6-35B-A3B-FP8"
    # Qdrant: service name on the Docker network
    qdrant_host: str = "qdrant"
    qdrant_port: int = 6333

    # Phase 5 MSY-02: cosine similarity threshold for MERGE badge
    # Plan 05-05 consumes this in _find_merge_candidate; tunable post-soak.
    memory_merge_threshold: float = 0.9

    # Phase 5 MSY-04: embedding retry loop tick interval (seconds).
    # EmbeddingRetryLoop._run_loop awakens every N seconds to drain the
    # mc:embeddings:retry Redis LIST. Tests override to 99999 (conftest.py)
    # so the loop never auto-fires; tests call _drain_once() directly.
    embedding_retry_interval: int = 60

    # Operational Controls
    enforce_dispatch_attempt_id: bool = True  # Phase B: active — hard 409 on missing/wrong header

    # Pre-Dispatch Gating (Phase 1 Systemic Orchestration)
    # False = legacy: dispatch_phase ignored, tasks dispatch immediately
    # True = tasks with dispatch_phase="planning" are NOT auto-dispatched
    enable_dispatch_gating: bool = False

    # Promote Orchestrator (Phase 4A)
    # False = planned tasks stay put until manually promoted
    # True = system makes auto-promote/approval/wait decisions every 30s
    enable_promote_orchestrator: bool = False

    # Structured Intake (Phase 2)
    # False = only the existing text box, new intake fields are ignored
    # True = Quick Mode + Structured Mode active, planning brief for the board lead
    structured_intake_enabled: bool = False

    # App
    environment: str = "development"
    # Matches the public release tag (CHANGELOG.md / GitHub Releases).
    # Release process: bump here + pyproject.toml + CHANGELOG, then tag.
    app_version: str = "0.1.1"
    # Fallback working directory for tasks WITHOUT project context.
    # Primarily dispatch.py uses project.workspace_path (via default_project_id on board).
    # mc_repo_path only kicks in when there is no project and no agent workspace.
    # Default derives from the host home (HOME_HOST in Docker) — override via
    # MC_REPO_PATH env var (setup.sh writes the actual checkout path).
    mc_repo_path: str = str(
        Path(os.environ.get("HOME_HOST", str(Path.home())))
        / "Workspace" / "Projects" / "mission-control"
    )

    # Free-Code Agent: base directory for task isolation (worktrees or plain workspaces)
    # In the container: /home/mcuser/free-code-projects (mounted from the host,
    # see docker-compose.override.example.yml)
    free_code_projects_path: str = "/home/mcuser/free-code-projects"

    # Free-Code Bridge: HTTP endpoint on the host (outside Docker)
    free_code_bridge_url: str = "http://host.docker.internal:18792"

    # Free-Code path mapping: Docker path → host path (for bridge requests)
    # Multiple mappings as a semicolon-separated list: "docker_path:host_path;..."
    # Order: longest paths first for correct replacement.
    # Default derives from the host home — override via FREE_CODE_PATH_MAPPINGS.
    free_code_path_mappings: str = (
        "/home/mcuser/free-code-projects:"
        + str(Path(os.environ.get("HOME_HOST", str(Path.home()))) / "FreeCode" / "projects")
    )

    # News-Site Export (optional): absolute host path of the news repo that
    # /api/v1/news/deploy exports to + pushes. Empty = deploy endpoint disabled.
    news_repo_path: str = ""

    # SSE keepalive interval (seconds)
    sse_ping_interval: int = 15

    # Agent token settings
    agent_token_iterations: int = 200_000

    # Intelligence Service
    ollama_url: str = "http://host.docker.internal:11434"
    intelligence_interval: int = 600  # 10 minutes — MEM-05 reduces overlap risk

    # Runtime & Model Management v1 (ADR-054) — background /v1/models probing.
    runtime_watcher_enabled: bool = True
    runtime_watcher_interval: int = 90  # seconds between probe ticks

    # CLI Tool Updates — periodic check of installed vs. pinned vs. latest
    # upstream CLI tool versions (openclaude/claude/omp). 0 = disabled.
    cli_update_check_interval: int = 21600  # 6 hours

    # Lifecycle Safety Watchdog (ADR-046) — global kill-switch for the
    # silent-abort auto-block check (task_runner._check_stuck_in_progress).
    # The idle THRESHOLD stays per-agent in agents.dispatch_config
    # ("stuck_block_minutes", ADR-031 pattern); only the on/off lives here so
    # the whole check can be disabled fleet-wide without a redeploy of config.
    lifecycle_watchdog_enabled: bool = True

    # File Indexer — periodic walk of browsable ~/.mc roots into file_index.
    # Accelerator only (bytes always stream live). Tests override to 99999 so
    # the loop never auto-fires; they call run_once() directly.
    file_index_interval: int = 600

    # Host-agent provisioning autoload (onboarding wizard, 2026-07-10). When
    # False (default), host provisioning only STAGES plist/env/run.sh into
    # ~/.mc/agents/<slug>/ and returns the launchctl command for the operator
    # to run + verify manually. When True, provisioning also copies the plist
    # into ~/Library/LaunchAgents and runs `launchctl bootstrap`. Kept off by
    # default because loading a launchd job is an irreversible host action.
    host_agent_autoload_enabled: bool = False

    # Phase 3 — Claude-Process Recycler (MEM-01)
    # Global kill-switch. Per-agent override lives in agents.recycler_enabled.
    # See ADR-024 + .planning/phases/03-memory-leak-root-cause-fix/.
    agent_recycler_enabled: bool = True

    # CTX-02 (Phase 6) — Compaction kill-switch. When False, the watchdog
    # falls through without compacting (agents stall at token limit but no
    # forced session reset happens). Default True; flip to False via env var
    # CONTEXT_COMPACTION_ENABLED=false + `docker compose restart backend` for
    # rollback. See ADR-026 / Plan 06-04.
    context_compaction_enabled: bool = True

    # Phase 7 OBS-02: Obsidian view-only export interval (seconds).
    # ObsidianExportService walks board_memory every N seconds and renders
    # Markdown into ${HOME_HOST}/.mc/vault/. Tests override to 99999
    # (conftest.py) so the loop never auto-fires.
    obsidian_export_interval: int = 300

    # Phase 7 OBS-02 — Deprecated in M.2 (Vault is now Source of Truth).
    # Default False — obsidian_export does NOT run alongside the Vault
    # Migration + Watcher + Compactor. Set OBSIDIAN_EXPORT_ENABLED=true +
    # `docker compose restart backend` to re-enable for rollback only.
    obsidian_export_enabled: bool = False

    # Remote runtime host SSH (optional — e.g. a DGX box running vLLM/LM Studio).
    # Empty = feature unused. Set DGX_SSH_HOST/DGX_SSH_USER in .env and mount
    # your SSH key (see docker-compose.override.example.yml).
    dgx_ssh_host: str = ""
    dgx_ssh_user: str = ""
    dgx_ssh_key_path: str = "/home/mcuser/.ssh/id_rsa"

    # ── PORSCHE power-managed runtime (unsloth_porsche) ───────────────────────
    # The PORSCHE Windows box runs a local unsloth OpenAI server but sleeps when
    # idle. Controlled via its Flask :5555 server (PowerShell) and woken via
    # Wake-on-LAN. These are fallback defaults — the authoritative values live on
    # the runtime row (control_url / wol_mac_address / endpoint / host).
    porsche_lan_ip: str = ""
    porsche_mac: str = ""
    porsche_broadcast: str = "192.0.2.255"
    porsche_control_url: str = ""
    # Directory (under the ~/.mc host bind-mount, same absolute path in Docker via
    # HOME_HOST) where the backend drops Wake-on-LAN trigger files for the
    # host-side launchd watcher. Backend can't send L2 broadcast from Docker, so
    # it hands the wake off to a watcher on the Mac host. See docs WoL host-helper.
    wake_request_dir: Path = Path(os.environ.get("HOME_HOST", str(Path.home()))) / ".mc" / "wake-requests"
    # Kill-switch + cache for the runtime-readiness dispatch gate. The gate only
    # ever affects agents bound to a power_managed runtime; every other agent
    # takes the unchanged dispatch path regardless of this flag.
    enable_runtime_readiness_gate: bool = True
    runtime_readiness_cache_ttl: int = 15  # seconds — avoids hammering :5555 on every poll

    # Vault Memory (M.1 Read Foundation)
    # Root of the Markdown Vault. Source of Truth for agent memory.
    # In Docker, HOME_HOST is set to the host's $HOME so the watcher sees Phase 7's writes.
    vault_path: Path = Path(os.environ.get("HOME_HOST", str(Path.home()))) / ".mc" / "vault"

    # Vault Index Rebuild on Boot
    # False (default): only rebuild on first boot when .mc_index.db is missing.
    # True: force rebuild on every backend start (useful after schema migrations
    # or manual vault edits). Flip via env var VAULT_INDEX_REBUILD_ON_BOOT=true
    # + `docker compose restart backend`.
    vault_index_rebuild_on_boot: bool = False

    # Vault Lint Cron Interval (hours)
    # M.3 Task 4: how often the vault_lint loop runs (orphans, invalid
    # frontmatter, duplicate IDs → daily report under `_lint/YYYY-MM-DD.md`).
    # Default 24h. Override via env var VAULT_LINT_INTERVAL_HOURS=<int> +
    # `docker compose restart backend`. The loop sleeps first then runs, so
    # restart-storms do not re-lint repeatedly. Tests can set this huge to
    # ensure the loop never fires during the suite.
    vault_lint_interval_hours: int = 24

    # Token Harvester (Phase 31)
    # Base paths for agent JSONL transcripts (cli-bridge + sparky + hermes).
    # Default: ~/.mc/agents (expanduser happens in the harvester).
    # Boss path (~/.claude/projects) is separately hardcoded in the harvester and
    # gets mounted into the container as :ro via docker-compose.yml.
    token_harvest_paths: list[str] = ["~/.mc/agents"]

    # Token Harvester — Grok source (Bench #18 PR1). Grok Build CLI (host
    # harness, ADR-066) logs turn-level usage to unified.jsonl (append-only,
    # same offset-resume mechanics as the JSONL sources above) and writes one
    # summary.json + prompt_history.jsonl per cwd under sessions_path (model/
    # cwd/task_id join). Both default relative to the host home like the
    # paths above — expanduser happens in the harvester.
    grok_harvest_path: str = "~/.grok/logs/unified.jsonl"
    grok_sessions_path: str = "~/.grok/sessions"

    # Token Harvester — Hermes source (Bench #18 PR1). Hermes (host harness)
    # keeps its own sqlite session ledger. NEVER opened live — the harvester
    # copies state.db (+ -wal/-shm if present) to a temp dir first. Only the
    # three files are mounted into the backend container (see docker-compose.yml
    # comment) — the rest of ~/.hermes/ holds secrets (.env, auth.json).
    hermes_state_db_path: str = "~/.hermes/state.db"


settings = Settings()


def phone_test_url() -> str:
    """Externally reachable URL for 'test on your phone' Telegram links.

    Configured via PUBLIC_HOST (ADR-035), not a hardcoded Tailscale IP. Falls
    back to mc_base_url when PUBLIC_HOST is unset.
    """
    return f"http://{settings.public_host}" if settings.public_host else settings.mc_base_url


def effective_host_ssh_user() -> str:
    """macOS login for host SSH / launchctl GUI-domain calls.

    Explicit HOST_SSH_USER wins; otherwise derived from the HOME_HOST
    basename (macOS: /Users/<login> is owned by that login).
    """
    return settings.host_ssh_user or Path(settings.home_host).name


# Placeholder values that must never survive into a production boot: the
# compose default, the .env.example sample, and common "fill me in" variants.
_PLACEHOLDER_JWT_SECRETS = frozenset(
    {
        "",
        "change-me",
        "change-me-in-production",
        "change_me_generate_with_openssl_rand_hex_32",
    }
)


def validate_boot_secrets(s: Settings | None = None) -> None:
    """Fail fast when production boots with placeholder/missing secrets.

    A bare `docker compose up` without ./setup.sh (e.g. a Portainer
    git-stackfile deploy) used to run with the default JWT secret — anyone
    who could reach the API could forge admin tokens. Called at the top of
    the lifespan; development/test environments are exempt.
    """
    s = s or settings
    if s.environment != "production":
        return
    problems: list[str] = []
    if s.jwt_secret_key in _PLACEHOLDER_JWT_SECRETS:
        problems.append(
            "JWT_SECRET_KEY is unset or a placeholder — anyone could forge "
            "admin login tokens. Generate one: openssl rand -hex 32"
        )
    if not s.secrets_encryption_key:
        problems.append(
            "SECRETS_ENCRYPTION_KEY is empty — the encrypted secrets vault "
            "(LLM provider keys) cannot work. Any stable passphrase is "
            "accepted; best: openssl rand -hex 32"
        )
    if problems:
        raise RuntimeError(
            "Refusing to start with insecure configuration:\n- "
            + "\n- ".join(problems)
            + "\nFix: run ./setup.sh (generates all secrets into .env), "
            "then `docker compose up -d` again."
        )

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

    # Telegram Reports-Bot (separat — Agent-Deliverables, kein Approval-Flow)
    # Zweiter Bot + Chat damit Info-Delivery nicht den Kommando-Chat verrauscht.
    telegram_reports_bot_token: str = ""
    telegram_reports_chat_id: str = ""

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

    # Subagent Dispatch (chat_send_isolated statt chat_send fuer Workers)
    # Kill-Switch: USE_SUBAGENT_DISPATCH=false in .env → sofort Legacy-Modus
    use_subagent_dispatch: bool = True

    # Reflection-Pflicht vor Task-Abschluss (Boss-Autonomy-Overhaul 2026-04-11)
    # True = letzter eigener Kommentar vor status=review/done muss comment_type=reflection sein
    # Eingeschaltet Phase E (2026-04-12) nach Worker-SOUL-Audit
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
    # Qdrant: Service-name im Docker-Netzwerk
    qdrant_host: str = "qdrant"
    qdrant_port: int = 6333

    # Phase 5 MSY-02: Cosine-Similarity-Threshold fuer MERGE-Badge
    # Plan 05-05 consumes this in _find_merge_candidate; tunable post-soak.
    memory_merge_threshold: float = 0.9

    # Phase 5 MSY-04: Embedding-Retry-Loop tick interval (Sekunden).
    # EmbeddingRetryLoop._run_loop awakens every N seconds to drain the
    # mc:embeddings:retry Redis LIST. Tests override to 99999 (conftest.py)
    # so the loop never auto-fires; tests call _drain_once() directly.
    embedding_retry_interval: int = 60

    # Operational Controls
    enforce_dispatch_attempt_id: bool = True  # Phase B: aktiv — harter 409 bei fehlendem/falschem Header

    # Pre-Dispatch Gating (Phase 1 Systemic Orchestration)
    # False = Legacy: dispatch_phase ignoriert, Tasks dispatchen sofort
    # True = Tasks mit dispatch_phase="planning" werden NICHT auto-dispatched
    enable_dispatch_gating: bool = False

    # Promote Orchestrator (Phase 4A)
    # False = geplante Tasks bleiben liegen bis manuell promoted
    # True = System trifft Auto-Promote/Approval/Wait-Entscheidungen alle 30s
    enable_promote_orchestrator: bool = False

    # Structured Intake (Phase 2)
    # False = nur bestehende Textbox, neue Intake-Felder werden ignoriert
    # True = Quick Mode + Structured Mode aktiv, Planning Brief fuer Henry
    structured_intake_enabled: bool = False

    # App
    environment: str = "development"
    # Entspricht dem Public-Release-Tag (CHANGELOG.md / GitHub Releases).
    # Release-Prozess: hier + pyproject.toml + CHANGELOG bumpen, dann taggen.
    app_version: str = "0.1.1"
    # Fallback-Arbeitsverzeichnis fuer Tasks OHNE Projekt-Kontext.
    # Primaer nutzt dispatch.py project.workspace_path (via default_project_id auf Board).
    # mc_repo_path greift nur wenn kein Projekt und kein Agent-Workspace vorhanden ist.
    # Default derives from the host home (HOME_HOST in Docker) — override via
    # MC_REPO_PATH env var (setup.sh writes the actual checkout path).
    mc_repo_path: str = str(
        Path(os.environ.get("HOME_HOST", str(Path.home())))
        / "Workspace" / "Projects" / "mission-control"
    )

    # Free-Code Agent: Basis-Verzeichnis fuer Task-Isolation (Worktrees oder Plain Workspaces)
    # Im Container: /home/mcuser/free-code-projects (gemountet vom Host,
    # siehe docker-compose.override.example.yml)
    free_code_projects_path: str = "/home/mcuser/free-code-projects"

    # Free-Code Bridge: HTTP-Endpunkt auf dem Host (ausserhalb Docker)
    free_code_bridge_url: str = "http://host.docker.internal:18792"

    # Free-Code Path-Mapping: Docker-Pfad → Host-Pfad (fuer Bridge-Requests)
    # Mehrere Mappings als Semikolon-getrennte Liste: "docker_path:host_path;..."
    # Reihenfolge: laengste Pfade zuerst fuer korrekte Ersetzung.
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
    intelligence_interval: int = 600  # 10 Minuten — MEM-05 reduces overlap risk

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
    # Basis-Pfade fuer Agent-JSONL-Transkripte (cli-bridge + sparky + hermes).
    # Standard: ~/.mc/agents (expanduser passiert im Harvester).
    # Boss-Pfad (~/.claude/projects) ist separat hartkodiert im Harvester und
    # wird via docker-compose.yml als :ro in den Container gemountet.
    token_harvest_paths: list[str] = ["~/.mc/agents"]


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

"""Migration 0167 — repair drifted runtime display names.

Runs the real ``upgrade()`` against an in-memory SQLite table shaped like the
columns it touches (same shim idea as ``test_migration_0121_task_comments_fk``,
but this migration is pure SQL/UPDATE, so it can actually be EXECUTED rather
than only inspected).

The fixture rows are the live registry of 2026-07-28 — the drifted labels, the
raw catalog-bound ones, and the curated local runtimes that must survive
untouched.
"""

from __future__ import annotations

import contextlib
import importlib.util
import pathlib
import types

import pytest
import sqlalchemy as sa

REVISION_PATH = (
    pathlib.Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "0167_runtime_display_name_derived.py"
)

# slug, display_name, endpoint, model_identifier, runtime_type
LIVE_ROWS = [
    ("gemma4-nvfp4", "Gemma 4 31B NVFP4", "http://192.0.2.10:8002/v1", None, "vllm_docker"),
    ("qwen-general", "Spark vLLM (Laguna/Qwen — switchable)",
     "http://192.0.2.10:8000/v1", "poolside/Laguna-S-2.1-NVFP4", "vllm_docker"),
    ("nemotron-super", "Nemotron 3 Super", "http://192.0.2.10:1234/v1", None, "lmstudio"),
    ("qwen-coder-lms", "Qwen3.6 35B A3B (LM Studio)",
     "http://192.0.2.10:1234/v1", None, "lmstudio"),
    ("unsloth-studio", "Unsloth Studio (DGX)", "http://192.0.2.10:8888", None, "unsloth"),
    ("ollama-cloud", "Ollama Cloud (glm-5.1)", "https://ollama.com/v1", "glm-5.1", "cloud"),
    ("anthropic-claude-opus", "Claude Opus 4.7 (Anthropic Pro/Max)",
     "https://api.anthropic.com/v1/messages", "claude-opus-4-8", "cloud"),
    ("anthropic-claude-sonnet", "Claude Sonnet 4.6 (Anthropic Pro/Max)",
     "https://api.anthropic.com/v1/messages", "claude-sonnet-5", "cloud"),
    ("hermes-vllm", "Hermes (Local Ollama → Cloud DeepSeek v4 Pro)",
     "http://127.0.0.1:11434/v1", "deepseek-v4-pro:cloud", "hermes"),
    ("unsloth-porsche", "Unsloth (PORSCHE)",
     "http://192.0.2.20:8000/v1", "gemma-4-26B-A4B-it-qat", "unsloth_porsche"),
    ("grok-cloud", "Grok Build (xAI Cloud, grok-4.5)",
     "https://cli-chat-proxy.grok.com", "grok-4.5", "grok"),
    ("kimi-cloud", "Kimi Code (Moonshot Cloud, K3)",
     "https://api.kimi.com/coding/v1", "kimi-code/k3", "kimi"),
    ("anthropic-claude-opus-5", "claude-opus-5",
     "https://api.anthropic.com/v1/messages", "claude-opus-5", "cloud"),
    ("ollama-cloud-glm-5-2", "glm-5.2", "https://ollama.com/v1", "glm-5.2", "cloud"),
    ("omp-qwen", "omp headless (Qwen)",
     "http://192.0.2.10:8000/v1", "poolside/Laguna-S-2.1-NVFP4", "omp"),
]

EXPECTED_AFTER = {
    # Derived — provider-backed cloud rows.
    "ollama-cloud": "GLM 5.1 (Ollama Cloud)",
    "anthropic-claude-opus": "Claude Opus 4.8 (Anthropic Pro/Max)",
    "anthropic-claude-sonnet": "Claude Sonnet 5 (Anthropic Pro/Max)",
    "grok-cloud": "Grok 4.5 (xAI Cloud)",
    "kimi-cloud": "Kimi Code K3 (Moonshot Cloud)",
    "anthropic-claude-opus-5": "Claude Opus 5 (Anthropic Pro/Max)",
    "ollama-cloud-glm-5-2": "GLM 5.2 (Ollama Cloud)",
}


@contextlib.contextmanager
def _migration_bound_to(conn):
    """Load the migration with `alembic.op` bound to a live connection.

    `backend/alembic/` (the migration directory) shadows the installed alembic
    package whenever pytest runs from `backend/`, so a real MigrationContext is
    not reachable here — the same reason test_migration_0091 shims `op`. This
    migration only ever calls `op.get_bind()`, so a one-attribute shim is a
    faithful stand-in and lets the REAL upgrade() SQL run against SQLite.
    """
    import alembic as _alembic

    previous = getattr(_alembic, "op", None)
    _alembic.op = types.SimpleNamespace(get_bind=lambda: conn)
    try:
        spec = importlib.util.spec_from_file_location("mc_migration_0167", REVISION_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        if previous is None:
            delattr(_alembic, "op")
        else:
            _alembic.op = previous


def _load_migration():
    with _migration_bound_to(None) as module:
        return module


@pytest.fixture()
def engine():
    eng = sa.create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE runtimes ("
                " slug TEXT PRIMARY KEY,"
                " display_name TEXT,"
                " endpoint TEXT,"
                " model_identifier TEXT,"
                " runtime_type TEXT)"
            )
        )
        for row in LIVE_ROWS:
            conn.execute(
                sa.text(
                    "INSERT INTO runtimes VALUES "
                    "(:slug, :display_name, :endpoint, :model_identifier, :runtime_type)"
                ),
                dict(zip(
                    ["slug", "display_name", "endpoint", "model_identifier", "runtime_type"],
                    row,
                )),
            )
    return eng


def _run_upgrade(engine) -> dict[str, str]:
    with engine.begin() as conn:
        with _migration_bound_to(conn) as module:
            module.upgrade()
        return {
            r[0]: r[1] for r in conn.execute(sa.text("SELECT slug, display_name FROM runtimes"))
        }


def test_revision_chains_onto_the_previous_head():
    module = _load_migration()
    assert module.revision == "0167_runtime_display_name_derived"
    assert module.down_revision == "0166_thread_telegram_topic_id"


def test_migration_renames_only_the_provider_backed_rows(engine):
    after = _run_upgrade(engine)
    before = {row[0]: row[1] for row in LIVE_ROWS}

    for slug, expected in EXPECTED_AFTER.items():
        assert after[slug] == expected, slug

    untouched = set(before) - set(EXPECTED_AFTER)
    for slug in untouched:
        assert after[slug] == before[slug], f"{slug} must keep its curated name"


def test_no_row_lies_about_its_model_after_the_migration(engine):
    """The drift gate over the WHOLE registry, not only the derivable rows.

    `tests/test_runtime_naming.py` runs the same check over config/runtimes.json
    (a fresh install); this one runs it over the live row set of 2026-07-28
    after the repair — derived, curated and NULL-model rows alike. Rows without
    a model_identifier are skipped by `display_name_drift` itself: there is
    nothing to compare them against.
    """
    from app.services.runtime_naming import display_name_drift

    after = _run_upgrade(engine)
    models = {row[0]: row[3] for row in LIVE_ROWS}
    offenders = {
        slug: display_name_drift(name, models[slug])
        for slug, name in after.items()
        if display_name_drift(name, models[slug])
    }
    assert offenders == {}


def test_the_two_lying_names_are_the_point(engine):
    """Both drifted rows carried a version number of a DIFFERENT real model."""
    after = _run_upgrade(engine)
    assert "4.7" not in after["anthropic-claude-opus"]
    assert "4.6" not in after["anthropic-claude-sonnet"]


def test_migration_is_idempotent(engine):
    first = _run_upgrade(engine)
    second = _run_upgrade(engine)
    assert first == second


def test_slugs_are_never_rewritten(engine):
    """Slugs appear in configs, skills and docs — renaming them for cosmetics
    would be a real outage risk. Only display_name moves."""
    with engine.begin() as conn:
        with _migration_bound_to(conn) as module:
            module.upgrade()
        slugs = {r[0] for r in conn.execute(sa.text("SELECT slug FROM runtimes"))}
    assert slugs == {row[0] for row in LIVE_ROWS}


def test_downgrade_is_a_no_op_and_loses_nothing(engine):
    """Downgrade deliberately does not restore the old labels: two of them were
    factually wrong and none is recoverable from any column. It must therefore
    leave the data alone rather than write a guess."""
    after = _run_upgrade(engine)
    with engine.begin() as conn:
        with _migration_bound_to(conn) as module:
            module.downgrade()
        final = {
            r[0]: r[1] for r in conn.execute(sa.text("SELECT slug, display_name FROM runtimes"))
        }
    assert final == after

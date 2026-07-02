"""Phase D — verify TOOLS.md + SOUL.md gain Files/Search/Related/task_id docs.

The deliverable wrapper feature (Phases A/B.1/E) shipped backend + voice
already. Phase D teaches the 10 Docker agents about it via TOOLS.md AND
SOUL.md:

  - search hits include type=deliverable wrappers
  - wrappers + attachments are read natively via /vault/agents/... and
    /vault/attachments/{files,images}/...
  - the existing POST /agent/vault/note accepts task_id (Phase E) — empfohlen
    bei task-bound writes
  - GET /agent/vault/related/{task_id} for the task-klammer query

These tests pin the doc strings so a future refactor of the vault section
can't silently drop them.
"""
import uuid

from app.models.agent import Agent
from app.scopes import Scope
from app.services.template_renderer import build_agent_context, render_agent_file
from app.services.tools_md_builder import generate_tools_md


def _gen(scopes: list[str]) -> str:
    return generate_tools_md(
        name="Researcher",
        emoji="🔎",
        raw_token="tok",
        board_id="board-uuid-123",
        is_board_lead=False,
        scopes=scopes,
    )


def _soul(scopes: list[str]) -> str:
    agent = Agent(
        id=uuid.uuid4(),
        name="Researcher",
        role="researcher",
        emoji="🔎",
        board_id=uuid.uuid4(),
        is_board_lead=False,
        scopes=scopes,
    )
    ctx = build_agent_context(agent, agents_on_board=[])
    return render_agent_file("SOUL.md.j2", ctx)


def test_vault_section_documents_deliverable_search_filter():
    """Search example must show how to filter for deliverable wrappers."""
    out = _gen([Scope.VAULT_READ.value, Scope.VAULT_WRITE.value])
    assert "type=deliverable" in out, "Missing deliverable-filter search example"


def test_vault_section_documents_native_read_paths():
    """Agents must learn the /vault/attachments/ and Read patterns."""
    out = _gen([Scope.VAULT_READ.value, Scope.VAULT_WRITE.value])
    assert "/vault/attachments/files/" in out
    assert "/vault/attachments/images/" in out
    assert "Read /vault/agents/" in out or "Read /vault/attachments/" in out


def test_vault_section_documents_auto_extracted_pdf_text():
    """PDFs have extracted text in the wrapper — agents shouldn't re-read the
    PDF if the wrapper already has what they need."""
    out = _gen([Scope.VAULT_READ.value, Scope.VAULT_WRITE.value])
    assert "Auto-extracted" in out


def test_vault_section_documents_task_id_on_note_post():
    """Phase E: vault note POST accepts task_id — must appear in the example."""
    out = _gen([Scope.VAULT_READ.value, Scope.VAULT_WRITE.value])
    # task_id appears as JSON field in the curl example
    assert '"task_id"' in out


def test_vault_section_documents_related_endpoint():
    """Task-klammer query endpoint must be discoverable from TOOLS.md."""
    out = _gen([Scope.VAULT_READ.value, Scope.VAULT_WRITE.value])
    assert "/agent/vault/related/" in out


def test_vault_section_documents_supersedes_convention():
    """Binary files are immutable — versioning happens via supersedes wrapper."""
    out = _gen([Scope.VAULT_READ.value, Scope.VAULT_WRITE.value])
    assert "supersedes" in out


def test_vault_section_omitted_without_scope():
    """Agent without vault scopes must NOT receive the vault section at all."""
    out = _gen([Scope.CHAT_WRITE.value, Scope.TASKS_READ.value])
    # The "Vault Files" subsection header is unique to the vault block
    assert "Vault Files" not in out
    assert "/vault/attachments/" not in out
    assert "/agent/vault/related/" not in out


# ── SOUL.md.j2 assertions (Phase D vault file disciplines) ───────────────────


def test_soul_vault_files_section_present_for_vault_write_agent():
    soul = _soul([Scope.VAULT_READ.value, Scope.VAULT_WRITE.value, Scope.HEARTBEAT.value])
    assert "### Vault Files (Deliverable-Wrappers)" in soul
    assert "/vault/attachments/files/" in soul
    assert "/vault/attachments/images/" in soul


def test_soul_task_klammer_section_present_for_vault_write_agent():
    soul = _soul([Scope.VAULT_READ.value, Scope.VAULT_WRITE.value, Scope.HEARTBEAT.value])
    assert "### Task-Bezug" in soul
    assert "/agent/vault/related/" in soul
    assert '"task_id"' in soul


def test_soul_vault_files_section_omitted_for_reader_only_agent():
    """vault:read only (no write) → Vault Files block is part of the existing
    vault:write gate, so it should not appear without vault:write."""
    soul = _soul([Scope.VAULT_READ.value, Scope.HEARTBEAT.value])
    assert "### Vault Files (Deliverable-Wrappers)" not in soul
    assert "### Task-Bezug" not in soul


def test_soul_vault_files_section_omitted_for_agent_without_vault_scopes():
    soul = _soul([Scope.CHAT_WRITE.value, Scope.TASKS_READ.value, Scope.HEARTBEAT.value])
    assert "Vault Files" not in soul
    assert "Task-Bezug" not in soul

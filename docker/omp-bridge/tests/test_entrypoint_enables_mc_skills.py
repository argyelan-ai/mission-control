"""omp must read the skills MC syncs into ~/.claude/skills.

plugin_manager.sync_agent_skills_to_disk copies an agent's allowed skills
(agents.cli_skills) into ~/.claude/skills. omp only reads that folder with
`skills.enableClaudeUser`, which ships OFF — so every skill assigned in MC
was invisible to omp agents. The entrypoint turns it on at every start.
"""
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parents[1] / "entrypoint.sh"


def _live_lines() -> list[str]:
    return [
        line.strip()
        for line in ENTRYPOINT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_entrypoint_turns_on_claude_user_skills():
    assert any(
        line.startswith("omp config set skills.enableClaudeUser true")
        for line in _live_lines()
    ), "entrypoint.sh must run `omp config set skills.enableClaudeUser true`"


def test_entrypoint_never_turns_skills_off():
    for line in _live_lines():
        assert "--no-skills" not in line
        assert "skills.enabled false" not in line
        assert "skills.enableClaudeUser false" not in line

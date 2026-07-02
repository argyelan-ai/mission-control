"""Tests for agent lessons rendering in dispatch messages."""
import pytest

from app.services.dispatch_message_builder import (
    DispatchSection,
    _assemble_with_budget,
    LESSON_AUTO_MAX_CHARS,
    render_agent_lessons_section,
)


class TestRenderAgentLessonsSection:
    def test_returns_none_when_no_lessons(self):
        """Empty lessons context produces no section."""
        result = render_agent_lessons_section("")
        assert result is None

    def test_renders_single_lesson(self):
        """Single lesson formatted correctly."""
        lessons_ctx = "- [Rate Limiting] xAI hat ein Limit von 10 RPM"
        section = render_agent_lessons_section(lessons_ctx)
        assert section is not None
        assert section.name == "agent_lessons"
        assert section.priority == 2
        assert "Deine bisherigen Erkenntnisse" in section.content
        assert "Rate Limiting" in section.content

    def test_respects_char_budget(self):
        """Section content stays within LESSON_AUTO_MAX_CHARS."""
        long_lessons = "\n".join(
            f"- [Lesson {i}] {'x' * 200}" for i in range(10)
        )
        section = render_agent_lessons_section(long_lessons)
        assert section is not None
        assert len(section.content) <= LESSON_AUTO_MAX_CHARS + 100  # header overhead

    def test_adds_search_hint_when_truncated(self):
        """When lessons are truncated, adds mc vault-search hint."""
        long_lessons = "\n".join(
            f"- [Lesson {i}] {'x' * 200}" for i in range(10)
        )
        section = render_agent_lessons_section(long_lessons)
        assert section is not None
        assert "mc vault-search" in section.content


class TestLessonsInBudget:
    def test_lessons_section_drops_under_budget_pressure(self):
        """Priority=2 lessons section drops when hard cap exceeded."""
        mandatory = DispatchSection(name="task", content="x" * 3800, priority=0)
        lessons = DispatchSection(name="agent_lessons", content="x" * 400, priority=2)
        result = _assemble_with_budget([mandatory, lessons])
        assert len(result) <= 4000
        # lessons should have been dropped
        assert result == mandatory.content

    def test_lessons_section_kept_within_budget(self):
        """Lessons section kept when budget allows."""
        mandatory = DispatchSection(name="task", content="x" * 1500, priority=0)
        lessons = DispatchSection(name="agent_lessons", content="LESSONS_HERE", priority=2)
        result = _assemble_with_budget([mandatory, lessons])
        assert "LESSONS_HERE" in result

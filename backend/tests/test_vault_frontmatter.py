from pathlib import Path
import pytest
from app.helpers.vault_frontmatter import (
    parse_frontmatter,
    validate_frontmatter,
    FrontmatterError,
)


def test_parse_valid_frontmatter(tmp_path):
    file = tmp_path / "lesson.md"
    file.write_text(
        "---\n"
        "id: 550e8400-e29b-41d4-a716-446655440000\n"
        "type: lesson\n"
        "agent: sparky\n"
        "date: 2026-05-14T15:42:01Z\n"
        "tags: [api, xai]\n"
        "---\n"
        "# Title\n\nBody."
    )
    post = parse_frontmatter(file)
    assert post.metadata["type"] == "lesson"
    assert post.metadata["agent"] == "sparky"
    assert post.metadata["tags"] == ["api", "xai"]
    assert "# Title" in post.content


def test_validate_required_fields():
    valid = {"id": "uuid", "type": "lesson", "agent": "sparky", "date": "2026-05-14T15:42:01Z"}
    validate_frontmatter(valid)  # should not raise

    missing_type = {"id": "uuid", "agent": "sparky", "date": "2026-05-14T15:42:01Z"}
    with pytest.raises(FrontmatterError, match="missing required field: type"):
        validate_frontmatter(missing_type)


def test_validate_type_enum():
    invalid_type = {"id": "uuid", "type": "BANANA", "agent": "sparky", "date": "2026-05-14T15:42:01Z"}
    with pytest.raises(FrontmatterError, match="invalid type"):
        validate_frontmatter(invalid_type)


def test_validate_date_iso8601():
    invalid_date = {"id": "uuid", "type": "lesson", "agent": "sparky", "date": "yesterday"}
    with pytest.raises(FrontmatterError, match="invalid date"):
        validate_frontmatter(invalid_date)


def test_parse_malformed_yaml_raises(tmp_path):
    file = tmp_path / "broken.md"
    file.write_text("---\nthis is: : broken: yaml: ::\n---\nbody")
    with pytest.raises(FrontmatterError, match="YAML parse error"):
        parse_frontmatter(file)

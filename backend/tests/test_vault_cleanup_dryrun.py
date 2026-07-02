import csv
import pytest
from app.services.vault_cleanup import dryrun_to_csv, NoteSample


def test_dryrun_writes_csv_with_classified_notes(tmp_path):
    notes = [
        NoteSample(path="a.md", agent="system", note_type="journal", tags=["auto"], content="x" * 200),
        NoteSample(path="b.md", agent="hermes", note_type="lesson", tags=["manual"], content="x" * 600),
        NoteSample(path="c.md", agent="tester", note_type="lesson", tags=[], content="x" * 200),
    ]
    out = tmp_path / "dryrun.csv"
    dryrun_to_csv(notes, out)
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 2  # b.md does not match any heuristic
    paths = {r["path"] for r in rows}
    assert paths == {"a.md", "c.md"}
    a_row = next(r for r in rows if r["path"] == "a.md")
    assert a_row["bucket"] == "H1"
    assert float(a_row["confidence"]) == pytest.approx(0.98)


def test_dryrun_respects_whitelist(tmp_path):
    notes = [
        NoteSample(path="keep-me.md", agent="system", note_type="journal", tags=["auto"], content="x" * 200),
    ]
    out = tmp_path / "dryrun.csv"
    dryrun_to_csv(notes, out, whitelist={"keep-me.md"})
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 0  # Whitelisted note excluded


def test_dryrun_csv_has_required_columns(tmp_path):
    notes = [
        NoteSample(path="a.md", agent="system", note_type="journal", tags=["auto"], content="x" * 200),
    ]
    out = tmp_path / "dryrun.csv"
    dryrun_to_csv(notes, out)
    reader = csv.DictReader(out.open())
    assert reader.fieldnames == ["path", "agent", "type", "length", "tags", "bucket", "confidence"]


def test_dryrun_returns_count_of_candidates(tmp_path):
    notes = [
        NoteSample(path="a.md", agent="system", note_type="journal", tags=["auto"], content="x" * 200),
        NoteSample(path="b.md", agent="hermes", note_type="lesson", tags=["manual"], content="x" * 600),
    ]
    out = tmp_path / "dryrun.csv"
    count = dryrun_to_csv(notes, out)
    assert count == 1

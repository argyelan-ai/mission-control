"""Phase 3 — Voice mc_client maps backend HTTPException details to reason codes.

The voice_worker function_tool ``deliver_to_telegram`` returns these to
Voice (xAI Grok); VOICE_INSTRUCTIONS then teaches Voice how to narrate
each one in German. Test the mapping in isolation — we don't need a
running httpx server for this, just verify the substring-match logic.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Same lazy-import dance as test_voice_worker_deliver — voice_worker is a
# sibling package, may not be on sys.path in CI environments.
VOICE_DIR = Path(__file__).resolve().parents[2] / "voice_worker"
if str(VOICE_DIR) not in sys.path:
    sys.path.insert(0, str(VOICE_DIR))


def _import_classifier():
    try:
        from mc_client import _classify_telegram_error  # type: ignore
    except ImportError as exc:
        pytest.skip(f"voice_worker not importable: {exc}")
    return _classify_telegram_error


def test_classifier_handles_none_and_empty():
    classify = _import_classifier()
    assert classify(None) == "unknown_error"
    assert classify("") == "unknown_error"


def test_file_too_large_from_telegram_reports():
    """telegram_reports.send_document returns description='file too large: ...'
    when >50MB; backend forwards it verbatim via HTTPException(422)."""
    classify = _import_classifier()
    detail = "file too large: 60_000_000 bytes exceeds Telegram limit"
    assert classify(detail) == "file_too_large"


def test_wrapper_not_found_404():
    classify = _import_classifier()
    assert classify("Vault-Wrapper nicht gefunden: agents/x/y.md") == "wrapper_not_found"


def test_wrapper_no_attachment_for_document_kind():
    """deliverable_kind in (document, url) → 400 with explanatory detail."""
    classify = _import_classifier()
    detail = "Wrapper hat keinen attachment_path — kinds 'document' und 'url' haben keine Binary"
    assert classify(detail) == "wrapper_no_attachment"


def test_attachment_missing_when_hardlink_broken():
    classify = _import_classifier()
    assert classify("attachment fehlt auf der Disk: ../../../attachments/files/x.pdf") == "attachment_missing"


def test_bot_unconfigured_503():
    classify = _import_classifier()
    assert classify("Reports-Bot nicht konfiguriert. Operator muss ...") == "bot_unconfigured"


def test_attachment_unsafe_path_traversal():
    classify = _import_classifier()
    assert classify("attachment_path verlaesst den Vault-Root") == "attachment_unsafe"


def test_input_mutex_when_both_deliverable_and_vault():
    classify = _import_classifier()
    detail = "deliverable_id (Photo), document_deliverable_id (File) und vault_path schliessen sich aus"
    assert classify(detail) == "input_mutex"


def test_text_too_long_at_4000_chars():
    classify = _import_classifier()
    assert classify("Telegram-Limit: max. 4000 Zeichen pro Message.") == "text_too_long"


def test_telegram_send_failed_generic_network():
    classify = _import_classifier()
    assert classify("Telegram-Send fehlgeschlagen (ConnectError). Retry moeglich.") == "telegram_send_failed"


def test_unknown_detail_falls_back():
    classify = _import_classifier()
    assert classify("totally unrelated backend gibberish") == "unknown_error"


def test_classifier_is_case_insensitive():
    """Defense against backend exception detail capitalization drift."""
    classify = _import_classifier()
    assert classify("FILE TOO LARGE: 60M") == "file_too_large"

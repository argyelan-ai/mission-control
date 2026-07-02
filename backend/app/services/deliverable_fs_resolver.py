"""Read-side path resolution for TaskDeliverables.

Thin re-export. The implementation now lives in ``fs_service`` (runtime-aware,
single containment guard, no any-absolute-path fallback). This module is kept
as a stable import name for existing callers (vault wrapper sync, etc.).
"""

from __future__ import annotations

from app.services.fs_service import resolve_deliverable as resolve_deliverable_fs_path

__all__ = ["resolve_deliverable_fs_path"]

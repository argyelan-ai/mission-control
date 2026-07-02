"""Phase B.1 — extract searchable text from binary deliverables.

Today: PDF text via pdfplumber (deterministic, local, no API call).

The extracted text replaces the placeholder `*(extraktion läuft...)*` line in
the wrapper's `## Auto-extracted` section. The vault-watcher picks up the
modify event and re-runs the embedding upsert → the operator's "wetter" search now
hits the PDF body even when the title doesn't mention weather.

Future kinds parked here for symmetry but NOT implemented at MVP time:
- images → xAI Grok vision (~$0.003/image, deferred per plan D4 review)
- audio  → whisper.cpp local on the M4 (no operator request yet)
"""

from __future__ import annotations

import logging
from pathlib import Path

import frontmatter as fm_lib
import pdfplumber

logger = logging.getLogger("mc.deliverable_extractor")


# Cap the extracted text per PDF. Reasoning:
# - The embedding model has a finite token window — feeding it the full body
#   of a 200-page report turns the vector into mush.
# - At ~5 chars/token, 50k chars ≈ 10k tokens, which still leaves headroom
#   for title + description + frontmatter to dominate the embedding signal.
# - If a PDF is genuinely longer than this, the first 50k chars usually carry
#   the executive summary / intro which is what semantic search needs.
PDF_TEXT_CHAR_CAP = 50_000

# Marker we leave in the wrapper's body during Phase A so the patcher knows
# where to drop the extracted text. Kept as a literal so a wrapper edited by
# the operator in the meantime won't be silently overwritten (the patcher matches the
# exact line and skips if it's missing).
EXTRACTION_PLACEHOLDER = "*(extraktion läuft...)*"


def extract_pdf_text(pdf_path: Path) -> str:
    """Extract concatenated text from a PDF. Returns empty string for any
    failure mode (encrypted, parse error, no text) — caller handles that as
    "skip, leave placeholder, log warning"."""
    if not pdf_path.exists():
        return ""

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            if getattr(pdf, "is_encrypted", False):
                logger.info("PDF %s is encrypted — skipping text extraction", pdf_path)
                return ""

            chunks: list[str] = []
            total = 0
            for page in pdf.pages:
                try:
                    text = page.extract_text() or ""
                except Exception as exc:
                    logger.debug("page extract failed in %s: %s", pdf_path, exc)
                    continue
                if not text:
                    continue
                chunks.append(text)
                total += len(text)
                if total >= PDF_TEXT_CHAR_CAP:
                    break
            joined = "\n\n".join(chunks).strip()
            return joined[:PDF_TEXT_CHAR_CAP]
    except Exception as exc:
        logger.warning("pdfplumber failed on %s: %s", pdf_path, exc)
        return ""


def patch_wrapper_with_extracted(wrapper_path: Path, extracted: str) -> bool:
    """Replace the `EXTRACTION_PLACEHOLDER` line in the wrapper with the
    extracted text. Returns True if the wrapper was actually rewritten.

    Atomic write: tmp + rename. If the wrapper has already been patched (no
    placeholder line present) we leave it alone — re-runs are idempotent.
    """
    if not wrapper_path.exists():
        return False

    try:
        post = fm_lib.load(str(wrapper_path))
    except Exception as exc:
        logger.warning("Cannot parse wrapper %s: %s", wrapper_path, exc)
        return False

    if EXTRACTION_PLACEHOLDER not in post.content:
        logger.debug("Wrapper %s has no placeholder — already patched or never had a PDF", wrapper_path)
        return False

    body = post.content.replace(EXTRACTION_PLACEHOLDER, extracted or "(no extractable text)")
    new_post = fm_lib.Post(body, **post.metadata)

    tmp = wrapper_path.with_suffix(wrapper_path.suffix + ".tmp")
    try:
        tmp.write_text(fm_lib.dumps(new_post))
        tmp.replace(wrapper_path)
        return True
    except OSError as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        logger.warning("Wrapper patch write failed for %s: %s", wrapper_path, exc)
        return False


def extract_and_patch(wrapper_path: Path, pdf_path: Path) -> str:
    """One-shot: extract text from PDF, patch wrapper, return extracted text.

    Used both from the sync hook (BackgroundTask in
    sync_deliverable_to_vault) and from the backfill CLI.

    Returns the extracted text so the caller can log how many chars landed.
    """
    text = extract_pdf_text(pdf_path)
    patch_wrapper_with_extracted(wrapper_path, text)
    return text

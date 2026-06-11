"""PDF text-layer access.

pdf_text returns the document's embedded text, or None when there is no
usable text layer (scans/photos, empty pages, unreadable files). None is
the signal for the OCR/vision path — which in this phase is an explicit
escalation, not a model call.
"""

from __future__ import annotations

from io import BytesIO

from pypdf import PdfReader


def pdf_text(data: bytes) -> str | None:
    """Extract the text layer; None = image-only/unreadable (OCR needed)."""
    try:
        reader = PdfReader(BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception:
        return None  # unreadable as PDF text — a human (or OCR) must look
    text = "\n".join(pages)
    return text if text.strip() else None

"""Document classification: deterministic first, model second, escalate last.

Exit 1 — the heuristics (filename patterns, mime, text-layer keywords)
score confidently: doc_type set, status='classified'. Deterministic code
decides whenever it can (principle 1: scripts extract).

Exit 2 — heuristics unsure: ask the LLMClassifier INTERFACE. No live
model exists in this phase; the default StubLLMClassifier abstains, and
tests inject canned classifiers.

Exit 3 — still ambiguous: status='escalated', reason='unclassifiable' —
into Carlos's queue, never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

import psycopg

DOC_TYPES: tuple[str, ...] = (
    "bank_statement", "receipt", "invoice", "contract", "other",
)

# keyword -> score, per type. Statement anchors are strong (they are the
# checksum vocabulary); generic words score low.
_TEXT_SIGNALS: dict[str, tuple[tuple[str, int], ...]] = {
    "bank_statement": (
        ("beginning balance", 3),
        ("ending balance", 3),
        ("statement period", 2),
        ("account number", 1),
        ("xxxx", 1),  # masked account numbers
        ("****", 1),
    ),
    "receipt": (
        ("subtotal", 2),
        ("change due", 3),
        ("cash tendered", 3),
        ("receipt", 2),
        ("merchant", 1),
        ("total", 1),
    ),
    "invoice": (
        ("invoice", 3),
        ("due date", 2),
        ("bill to", 2),
        ("remit to", 2),
        ("invoice number", 2),
    ),
    "contract": (
        ("agreement", 2),
        ("hereinafter", 3),
        ("the parties", 2),
        ("witnesseth", 3),
    ),
}
_FILENAME_SIGNALS: dict[str, tuple[str, ...]] = {
    "bank_statement": ("statement", "stmt"),
    "receipt": ("receipt", "rcpt"),
    "invoice": ("invoice", "inv-"),
    "contract": ("contract", "agreement"),
}
CONFIDENT_SCORE: int = 4  # top type needs this much…
CONFIDENT_MARGIN: int = 2  # …and this much daylight over the runner-up


@dataclass(frozen=True)
class ClassificationResult:
    doc_type: str | None
    confident: bool
    signals: list[str] = field(default_factory=list)


class LLMClassifier(Protocol):
    """Model-judgment seam. Returns a DOC_TYPES member or None (abstain)."""

    def classify(self, filename: str | None, text: str | None) -> str | None: ...


class StubLLMClassifier:
    """No model is wired in this phase — always abstains."""

    def classify(self, filename: str | None, text: str | None) -> str | None:
        return None


def heuristic_classification(
    filename: str | None, content_type: str | None, text: str | None
) -> ClassificationResult:
    scores: dict[str, int] = dict.fromkeys(_TEXT_SIGNALS, 0)
    signals: dict[str, list[str]] = {doc_type: [] for doc_type in _TEXT_SIGNALS}

    lowered_name = (filename or "").lower()
    for doc_type, needles in _FILENAME_SIGNALS.items():
        for needle in needles:
            if needle in lowered_name:
                scores[doc_type] += 3
                signals[doc_type].append(f"filename:{needle}")

    lowered_text = (text or "").lower()
    for doc_type, weighted in _TEXT_SIGNALS.items():
        for needle, weight in weighted:
            if needle in lowered_text:
                scores[doc_type] += weight
                signals[doc_type].append(f"text:{needle}")

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    (top_type, top_score), (_, second_score) = ranked[0], ranked[1]
    confident = (
        top_score >= CONFIDENT_SCORE
        and top_score - second_score >= CONFIDENT_MARGIN
    )
    return ClassificationResult(
        doc_type=top_type if confident else None,
        confident=confident,
        signals=signals[top_type] if confident else [],
    )


def classify_document(
    conn: psycopg.Connection,
    document_id: UUID,
    *,
    text: str | None,
    llm: LLMClassifier | None = None,
) -> str:
    """Classify one received document; returns the resulting status.

    text is the extracted text layer (None when there is none) — the
    caller owns PDF handling so this module stays dependency-free.
    """
    row = conn.execute(
        "SELECT filename, content_type, status FROM documents WHERE id = %s",
        (document_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"document {document_id} does not exist")
    filename, content_type, status = row
    if status != "received":
        raise ValueError(f"document {document_id} is {status!r}, not 'received'")

    result = heuristic_classification(filename, content_type, text)
    doc_type = result.doc_type
    if doc_type is None:
        doc_type = (llm or StubLLMClassifier()).classify(filename, text)
        if doc_type is not None and doc_type not in DOC_TYPES:
            raise ValueError(f"classifier returned unknown type {doc_type!r}")

    if doc_type is None:
        conn.execute(
            """
            UPDATE documents SET status = 'escalated',
                                 escalation_reason = 'unclassifiable'
            WHERE id = %s
            """,
            (document_id,),
        )
        conn.commit()
        return "escalated"

    conn.execute(
        "UPDATE documents SET status = 'classified', doc_type = %s WHERE id = %s",
        (doc_type, document_id),
    )
    conn.commit()
    return "classified"

"""Rule contract for the deterministic rules engine.

A rule is any object (normally a module under rules/) exposing:

    rule_code:   stable string ('R010') — part of the flag natural key,
                 never renamed once flags exist
    severity:    'info' | 'warn' | 'critical'
    title:       short human name
    description: WHY this rule exists — what error/fraud it catches
    run(conn, client_id, as_of) -> list[Finding]

Rules are pure SQL/Python over canonical tables — no LLM calls anywhere
in this layer (principle 1). Findings must point at the row they are
about (principle 2): the engine rejects findings without a source_ref —
validators enforce, not prompts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import psycopg

SEVERITIES: tuple[str, ...] = ("info", "warn", "critical")
SOURCE_TYPES: tuple[str, ...] = ("transaction", "account", "entity", "job", "client")

REQUIRED_ATTRS: tuple[str, ...] = (
    "rule_code",
    "severity",
    "title",
    "description",
    "run",
)


class InvalidRule(Exception):
    """A registered rule does not satisfy the rule contract."""


class UnsourcedFinding(Exception):
    """A rule emitted a finding without a source pointer (principle 2)."""


@dataclass(frozen=True)
class Finding:
    """One observation by one rule about one canonical row.

    source_ref is the canonical row's UUID as a string; source_type names
    the table family it points into. (client_id, rule_code, source_ref)
    is the deterministic natural key for flag lifecycle.
    """

    source_type: str
    source_ref: str
    detail: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Rule(Protocol):
    rule_code: str
    severity: str
    title: str
    description: str

    def run(
        self, conn: psycopg.Connection, client_id: UUID, as_of: date
    ) -> list[Finding]: ...


def validate_rule(rule: Any) -> None:
    """Reject malformed rules at registration time, not at flag-write time."""
    missing = [attr for attr in REQUIRED_ATTRS if not hasattr(rule, attr)]
    if missing:
        raise InvalidRule(f"rule {rule!r} is missing attributes: {missing}")
    if not str(rule.rule_code).strip():
        raise InvalidRule("rule_code must be a non-empty stable string")
    if rule.severity not in SEVERITIES:
        raise InvalidRule(
            f"rule {rule.rule_code}: severity {rule.severity!r} not in {SEVERITIES}"
        )
    if not callable(rule.run):
        raise InvalidRule(f"rule {rule.rule_code}: run is not callable")


def validate_finding(rule_code: str, finding: Finding) -> None:
    """No finding without a pointer — enforced, not requested."""
    if not str(finding.source_ref).strip():
        raise UnsourcedFinding(f"rule {rule_code}: finding has empty source_ref")
    if not str(finding.source_type).strip():
        raise UnsourcedFinding(f"rule {rule_code}: finding has empty source_type")

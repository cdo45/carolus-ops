"""Explicit rule registry — deterministic order, no discovery magic.

Add new rules here in code order; every entry is contract-validated at
import time so a malformed rule fails the build, not a nightly run.
"""

from __future__ import annotations

from types import ModuleType

from rules import r000_unbalanced_lines, r001_orphan_lines
from rules.base import validate_rule

ALL_RULES: tuple[ModuleType, ...] = (
    r000_unbalanced_lines,
    r001_orphan_lines,
)

for _rule in ALL_RULES:
    validate_rule(_rule)

"""Explicit rule registry — deterministic order, no discovery magic.

Add new rules here in code order; every entry is contract-validated at
import time so a malformed rule fails the build, not a nightly run.
"""

from __future__ import annotations

from types import ModuleType

from rules import (
    r000_unbalanced_lines,
    r001_orphan_lines,
    r010_duplicate_payment,
    r011_duplicate_bill,
    r012_round_number_je,
    r013_suspense_aging,
    r014_negative_expense_balance,
    r015_stale_uncategorized,
    r016_backdated_entry,
    r017_weekend_je,
    r020_vendor_spend_spike,
    r021_new_vendor_large,
    r022_payment_without_bill,
    r023_ar_concentration,
    r024_missing_vendor_on_spend,
)
from rules.base import validate_rule

ALL_RULES: tuple[ModuleType, ...] = (
    r000_unbalanced_lines,
    r001_orphan_lines,
    r010_duplicate_payment,
    r011_duplicate_bill,
    r012_round_number_je,
    r013_suspense_aging,
    r014_negative_expense_balance,
    r015_stale_uncategorized,
    r016_backdated_entry,
    r017_weekend_je,
    r020_vendor_spend_spike,
    r021_new_vendor_large,
    r022_payment_without_bill,
    r023_ar_concentration,
    r024_missing_vendor_on_spend,
)

for _rule in ALL_RULES:
    validate_rule(_rule)

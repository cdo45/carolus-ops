"""carolus analysis layer.

Adapters that feed the vendored KPI engine (``kpi_engine/``) from carolus's
canonical Postgres instead of the engine's CSV parsers. Part 1 covers the
chart-of-accounts + general-ledger feed and its trial-balance reconciliation
gate (see :mod:`analysis.engine_feed` and :mod:`analysis.reconcile`).

The engine is treated as a vendored black box: we populate the SQLite input
tables defined by ``kpi_engine/core/db.py`` and call its own classifier and
KPI plumbing — we never refactor its internal ``core.`` imports.
"""

from __future__ import annotations

"""Pytest configuration for carolus-ops.

`kpi_engine/` is a vendored, self-contained subtree (the QBO KPI engine) that
ships with its own top-level ``core`` package and its own test suite. It is
intentionally excluded from carolus-ops' root test collection: the engine's
tests do ``import core`` resolved against the engine root, which would fail (or
collide) under carolus-ops' ``pythonpath = ["."]`` layout.

Run the engine's tests deliberately instead:

    cd kpi_engine && pytest
"""

collect_ignore = ["kpi_engine"]

"""The Phase 4 gate must pass on a DIRTY database: two consecutive
fixture-mode runs against the same scratch DB, no cleanup in between —
the second run sees every document, flag, rec_run, and canonical txn the
first one left behind, and must still judge only its own artifacts."""

from __future__ import annotations

import pytest

from tests.gate_phase4 import main as gate_main


def test_fixture_gate_passes_twice_without_cleanup(
    scratch_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CAROLUS_TEST_DB", scratch_db_url)
    # the equality guard must compare against something different;
    # load_dotenv() inside main() does not override existing env vars
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused@nowhere/none")

    first = gate_main(["--fixture"])
    assert first == 0, f"first run failed:\n{capsys.readouterr().out}"

    second = gate_main(["--fixture"])
    assert second == 0, f"second (dirty) run failed:\n{capsys.readouterr().out}"

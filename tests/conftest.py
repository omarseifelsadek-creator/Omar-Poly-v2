"""
Shared test fixtures.

Every test runs with CSV logging redirected to a temp directory so the
suite NEVER writes into data/logs/ — those files are live experiment
data (paper/live session fills and settlements), and a synthetic test
row in them would contaminate baselines like EXP-002.
"""

import pytest

import execution.pair_logger as pair_logger


@pytest.fixture(autouse=True)
def hermetic_log_dir(tmp_path, monkeypatch):
    """Redirect all pair-CSV logging into the test's temp directory."""
    monkeypatch.setattr(pair_logger, "LOG_DIR", str(tmp_path / "logs"))
    yield

"""Shared test setup: isolate the index location.

InfoGrep stores indexes under $INFOGREP_HOME (never inside the indexed folder). Point it
at a per-test temp dir so tests are isolated and never touch the real ~/.infogrep.
"""

import pytest


@pytest.fixture(autouse=True)
def isolated_index_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("infogrep_home")
    monkeypatch.setenv("INFOGREP_HOME", str(home))
    # Searches stay in the test process: an `infogrep serve` that happens to be running
    # on this machine reads the real indexes, not the test's.
    monkeypatch.setenv("INFOGREP_SERVER", "")
    return home

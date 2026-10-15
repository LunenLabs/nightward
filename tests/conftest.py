"""Shared test fixtures."""
import pytest

from nightward import scrub


@pytest.fixture(autouse=True)
def _plain_output(monkeypatch):
    """Tests read CLI output as text. A developer shell or CI runner that forces
    colour (FORCE_COLOR, CLICOLOR_FORCE, TTY_COMPATIBLE) would make rich emit ANSI
    codes even into pipes, and every text assertion would fail for that reason."""
    for var in ("FORCE_COLOR", "CLICOLOR_FORCE", "TTY_COMPATIBLE", "TTY_INTERACTIVE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _isolate_scrubbers():
    """Custom scrubbers are module-global; reset around every test."""
    scrub._reset()
    yield
    scrub._reset()

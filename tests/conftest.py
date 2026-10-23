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
def _run_from_tmp_path(request, monkeypatch):
    """A test that writes a suite into tmp_path runs it from there, as a user
    runs nightward from their project. On a Windows runner the checkout is on D:
    and the temp dir on C:; a child pytest started from D: takes D: as its
    rootdir, walks C:\\ down through the shared temp dir while collecting, and
    fails when another process deletes a tmp* entry mid-walk."""
    if "tmp_path" in request.fixturenames:
        monkeypatch.chdir(request.getfixturevalue("tmp_path"))


@pytest.fixture(autouse=True)
def _isolate_scrubbers():
    """Custom scrubbers are module-global; reset around every test."""
    scrub._reset()
    yield
    scrub._reset()

"""[tool.nightward] fails closed: a key nightward doesn't know is an error, and a
project can require a minimum nightward version.

A typo (`pth`, `review.billing` for `review.groups.billing`) used to be ignored, so
the setting it meant - a capture path, an exhaustive group - silently did nothing.
An older nightward ignores keys a newer one added in the same way.
"""
import os
import subprocess
import sys

import pytest

from nightward import __version__
from nightward.config import capture_path, check_requires, review_depths
from nightward.errors import NightwardError


def write(tmp_path, text):
    (tmp_path / "pyproject.toml").write_text(text, encoding="utf-8")
    return tmp_path / ".nightward"


def cli(*args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "nightward", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ},
    )


def test_an_unknown_key_is_an_error_naming_the_close_one(tmp_path):
    store = write(tmp_path, '[tool.nightward]\npth = "tests"\n')
    with pytest.raises(NightwardError, match=r"unknown key 'pth'.*did you mean 'path'"):
        capture_path(store)


def test_a_group_depth_written_in_the_review_table_is_an_error(tmp_path):
    # the misplaced form: the group belongs under [tool.nightward.review.groups]
    store = write(tmp_path, '[tool.nightward.review]\nbilling = "exhaustive"\n')
    with pytest.raises(NightwardError,
                       match=r"\[tool\.nightward\.review\] unknown key 'billing'.*groups"):
        review_depths(store)


def test_every_documented_key_is_accepted(tmp_path):
    store = write(tmp_path, f"""
[tool.nightward]
judge = "persona:editor"
path = "tests"
domain = "docs/nightward-domain"
requires = "{__version__}"
coverage = "."

[tool.nightward.review]
default = "standard"

[tool.nightward.review.groups]
pay = "exhaustive"
""")
    assert capture_path(store) is not None
    assert review_depths(store).of("pay") == "exhaustive"


def test_a_newer_required_version_is_an_error(tmp_path):
    store = write(tmp_path, '[tool.nightward]\nrequires = "99.0"\n')
    with pytest.raises(NightwardError, match=rf"requires nightward >= 99\.0.*{__version__}"):
        check_requires(store)


def test_the_running_version_satisfies_its_own_floor(tmp_path):
    check_requires(write(tmp_path, f'[tool.nightward]\nrequires = "{__version__}"\n'))
    check_requires(write(tmp_path, '[tool.nightward]\nrequires = "0.3"\n'))
    check_requires(write(tmp_path, ""))


def test_requires_compares_numerically_not_as_text(tmp_path):
    # "0.10" > "0.9" as versions, < as strings
    with pytest.raises(NightwardError, match="requires nightward >= 99.10"):
        check_requires(write(tmp_path, '[tool.nightward]\nrequires = "99.10"\n'))


@pytest.mark.parametrize("bad", ['">=0.3"', '"latest"', "3", '""'])
def test_requires_must_be_a_plain_version(tmp_path, bad):
    with pytest.raises(NightwardError, match=r'requires must be a version.*requires = "'):
        check_requires(write(tmp_path, f"[tool.nightward]\nrequires = {bad}\n"))


@pytest.mark.parametrize("command", [["status"], ["gate"], ["approve", "--all"],
                                     ["review"], ["view"]])
def test_every_store_command_refuses_a_newer_requirement(tmp_path, command):
    write(tmp_path, '[tool.nightward]\nrequires = "99.0"\n')
    assert cli("init", cwd=tmp_path).returncode == 2
    (tmp_path / ".nightward").mkdir()
    r = cli(*command, cwd=tmp_path)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "requires nightward >= 99.0" in r.stdout + r.stderr


def test_the_mcp_server_refuses_to_start_under_a_newer_requirement(tmp_path):
    write(tmp_path, '[tool.nightward]\nrequires = "99.0"\n')
    (tmp_path / ".nightward").mkdir()
    r = cli("mcp", cwd=tmp_path)
    assert r.returncode == 2
    assert "requires nightward >= 99.0" in r.stdout + r.stderr

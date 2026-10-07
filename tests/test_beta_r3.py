"""Beta round 3: gate-integrity defects, frozen as tests.

Each test failed on the code before its fix. A failure here is a real defect -
fix the code, do not weaken the test.
"""
import json
import os
import subprocess
import sys

import pytest


def cli(*args, cwd, env=None):
    return subprocess.run(
        [sys.executable, "-m", "nightward", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, **(env or {})},
    )


def write(path, body):
    path.write_text(body, encoding="utf-8")


def baseline_names(tw):
    return sorted(f.name.split(".approved.json")[0] for f in (tw / "baseline").glob("*.json"))


# ---- R2-OPS-02 (reopened), R3-DATA-08 (D18): only a clean whole-suite run proves
# a removal -------------------------------------------------------------------

SVC_V1 = ('import pytest\n'
          'SERVICES = [{"name": "api", "replicas": 3}, {"name": "worker", "replicas": 2}]\n'
          '@pytest.mark.parametrize("svc", SERVICES)\n'
          'def test_render(behavior, svc):\n'
          '    behavior(f"svc.{svc[\'name\']}", {"replicas": svc["replicas"]}, group="r")\n')
SVC_V2 = ('import os, pytest\n'
          'NO_QUEUE = os.environ.get("NO_QUEUE") == "1"\n'
          'SERVICES = [{"name": "auth", "replicas": 1}, {"name": "api", "replicas": 3},\n'
          '            pytest.param({"name": "worker", "replicas": 2},\n'
          '                         marks=pytest.mark.skipif(NO_QUEUE, reason="no queue"))]\n'
          '@pytest.mark.parametrize("svc", SERVICES)\n'
          'def test_render(behavior, svc):\n'
          '    behavior(f"svc.{svc[\'name\']}", {"replicas": svc["replicas"]}, group="r")\n')


def test_shifted_parametrize_id_is_no_removal_proof(tmp_path):
    write(tmp_path / "test_svc.py", SVC_V1)
    tw = tmp_path / ".tw"
    cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    write(tmp_path / "test_svc.py", SVC_V2)
    cli("run", ".", "--dir", str(tw), cwd=tmp_path, env={"NO_QUEUE": "1"})
    r = cli("approve", "--all", "--include-removed", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "svc.worker" in baseline_names(tw)
    assert "1 skipped" in r.stdout


def test_capture_moved_into_a_skipped_test_in_one_change_is_kept(tmp_path):
    write(tmp_path / "test_a.py",
          'def test_one(behavior):\n'
          '    behavior("render.dev", "dev", group="r")\n'
          '    behavior("render.meta", 1, group="r")\n')
    tw = tmp_path / ".tw"
    cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    write(tmp_path / "test_a.py",
          'import pytest\n'
          'def test_one(behavior):\n'
          '    behavior("render.meta", 1, group="r")\n'
          '@pytest.mark.skip(reason="no schema")\n'
          'def test_dev(behavior):\n'
          '    behavior("render.dev", "dev", group="r")\n')
    cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    r = cli("approve", "--all", "--include-removed", "--dir", str(tw), cwd=tmp_path)
    assert "render.dev" in baseline_names(tw), r.stdout


@pytest.fixture
def legacy_pair(tmp_path):
    """Baselines from before `source` existed (R3-DATA-08)."""
    tw = tmp_path / ".nightward"
    (tw / "baseline").mkdir(parents=True)
    for name, value in (("a", 1), ("b", 2)):
        write(tw / "baseline" / f"{name}.approved.json",
              json.dumps({"group": "g", "name": name, "payload": value}, indent=2) + "\n")
        write(tmp_path / f"test_{name}.py",
              f'def test_{name}(behavior):\n    behavior("{name}", {value}, group="g")\n')
    return tmp_path, tw


@pytest.mark.parametrize("pytest_args, env", [
    (["--collect-only"], {}),
    (["--setup-plan"], {}),
    (["--ignore=test_b.py"], {}),
    ([], {"PYTEST_ADDOPTS": "--ignore=test_b.py"}),
])
def test_run_that_did_not_run_everything_proves_no_removal(legacy_pair, pytest_args, env):
    tmp_path, tw = legacy_pair
    cli("run", ".", "--", *pytest_args, cwd=tmp_path, env=env)
    r = cli("approve", "--all", "--include-removed", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert baseline_names(tw) == ["a", "b"], r.stdout
    assert "clean whole-suite run" in r.stdout


def test_direct_collect_only_capture_proves_no_removal(legacy_pair):
    tmp_path, tw = legacy_pair
    subprocess.run([sys.executable, "-m", "pytest", "--co", "-q", "--nightward-record",
                    "-p", "no:cacheprovider"], cwd=tmp_path, capture_output=True)
    cli("report", cwd=tmp_path)
    cli("review", cwd=tmp_path)
    r = cli("approve", "--all", "--include-removed", cwd=tmp_path)
    assert baseline_names(tw) == ["a", "b"], r.stdout + r.stderr


def test_clean_whole_suite_run_still_proves_a_legacy_removal(legacy_pair):
    tmp_path, tw = legacy_pair
    write(tmp_path / "test_b.py", "def test_b():\n    pass\n")
    cli("run", ".", cwd=tmp_path)
    r = cli("approve", "--all", "--include-removed", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert baseline_names(tw) == ["a"], r.stdout


def test_renamed_source_test_is_no_removal_proof(tmp_path):
    write(tmp_path / "test_a.py", 'def test_a(behavior):\n    behavior("a", 1)\n'
                                  'def test_b(behavior):\n    behavior("b", 2)\n')
    tw = tmp_path / ".tw"
    cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    write(tmp_path / "test_a.py", 'def test_a(behavior):\n    behavior("a", 1)\n'
                                  'def test_b2():\n    pass\n')
    cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    r = cli("approve", "--all", "--include-removed", "--dir", str(tw), cwd=tmp_path)
    assert "b" in baseline_names(tw)
    assert "test_a.py::test_b did not run" in r.stdout

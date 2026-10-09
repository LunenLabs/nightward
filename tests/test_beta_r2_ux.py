"""Beta round 2: dashboard/CLI UX defects reported by beta testers, frozen as tests.

Each test failed on the code before its fix. A failure here is a real defect -
fix the code, do not weaken the test.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nightward.core.baseline import Store
from nightward.core.behavior import Behavior, validate_name
from nightward.runner import recompute
from nightward.shellquote import SHELLS, command, quote
from nightward.view import collect_data

APP_JS = Path(__file__).parents[1] / "src" / "nightward" / "view" / "assets" / "app.js"

# Valid behavior names that are hostile or awkward in some shell.
EVIL = [
    "x;touch${IFS}pwned1.txt", "$(touch${IFS}pwned2.txt)", "`touch${IFS}pwned3.txt`",
    "search.q=a&page=2", "price(eur)", "GET.users.$id", "a'b", "a\u2019b", "a\u2018\u2018b",
    "x^y", "!!", "@splat", "a,b", "{a,b}", "~home", "#hash", "x&calc", "\uac00\uaca9.\u00e9",
    "a$env.PATH", "a$HOME.x", "100%", "%PATH%",
]
SAFE = ["checkout_total", "fx.table", "price.fr_FR", "metric_001", "a+b=c", "-dash"]

ARGV_PY = "import sys, json; print(json.dumps(sys.argv[1:]))"


def _posix_sh():
    if os.name != "nt":
        return shutil.which("sh")
    git_sh = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git" / "bin" / "sh.exe"
    return str(git_sh) if git_sh.exists() else None   # never WSL's bash.exe


def run_in_shell(shell, args_text, cwd):
    """argv a Python child receives when `python -c ... <args_text>` is typed into `shell`."""
    script = cwd / "argv.py"
    script.write_text(ARGV_PY, encoding="utf-8")
    py, sc = sys.executable, str(script)
    if shell == "posix":
        sh = _posix_sh()
        if not sh:
            pytest.skip("no POSIX sh")
        line = f"'{Path(py).as_posix()}' '{Path(sc).as_posix()}' {args_text}"
        r = subprocess.run([sh, "-c", line], cwd=cwd, capture_output=True)
    elif shell == "powershell":
        exe = shutil.which("pwsh") or shutil.which("powershell")
        if not exe:
            pytest.skip("no PowerShell")
        enc = base64.b64encode(f"& '{py}' '{sc}' {args_text}".encode("utf-16-le")).decode()
        r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                           cwd=cwd, capture_output=True)
    else:
        if os.name != "nt":
            pytest.skip("cmd.exe is Windows-only")
        r = subprocess.run(f'cmd.exe /d /c ""{py}" "{sc}" {args_text}"', cwd=cwd,
                           capture_output=True)
    out = r.stdout.decode("utf-8", errors="replace").strip().splitlines()
    return json.loads(out[-1]) if out else None


def breached_with(tw, names, removed=()):
    store = Store(tw)
    store.ensure()
    for n in [*names, *removed]:
        store.write_pending(Behavior(name=n, payload=1, group="g"))
        store.approve(n)
    store.clear_pending()
    for n in names:
        store.write_pending(Behavior(name=n, payload=2, group="g"))
    recompute(store)
    return store


# ---- R2-WEB-02: copy-paste commands are one literal argument per name ---------

@pytest.mark.parametrize("name", EVIL + SAFE)
def test_test_names_are_valid_behavior_names(name):
    validate_name(name)   # the threat is real only for names the plugin accepts


@pytest.mark.parametrize("shell", SHELLS)
def test_quoted_names_reach_the_program_literally_in_each_shell(tmp_path, shell):
    names = [n for n in EVIL + SAFE if quote(n, shell) is not None and not n.startswith("-")]
    got = run_in_shell(shell, " ".join(quote(n, shell) for n in names), tmp_path)
    assert got == names
    assert not list(tmp_path.glob("pwned*")), "a pasted name executed code"


def test_cmd_has_no_safe_form_for_percent_or_bang():
    assert quote("100%", "cmd") is None and quote("!!", "cmd") is None
    assert command("approve", ["ok", "100%"], "cmd") is None


def test_leading_dash_names_are_not_read_as_options():
    assert command("approve", ["-dash"], "posix") == "nightward approve -- -dash"


def test_dashboard_data_carries_quoted_names(tmp_path):
    breached_with(tmp_path / ".tw", ["x;touch${IFS}pwned1.txt", "plain"])
    quoted = collect_data(tmp_path / ".tw")["quoted"]
    assert quoted["plain"] == {"posix": "plain", "powershell": "plain", "cmd": "plain"}
    assert quoted["x;touch${IFS}pwned1.txt"]["posix"] == "'x;touch${IFS}pwned1.txt'"


def node_eval(js_expr):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    runner = (
        "const vm = require('vm'), fs = require('fs');"
        "const ctx = {fetch: () => new Promise(() => {}), console};"
        "vm.createContext(ctx); vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);"
        "process.stdout.write(JSON.stringify(vm.runInContext(process.argv[2], ctx)));"
    )
    r = subprocess.run([node, "-e", runner, str(APP_JS), js_expr], capture_output=True,
                       text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.parametrize("shell", SHELLS)
def test_dashboard_chips_use_the_quoted_form(shell):
    names = ["x;touch${IFS}pwned1.txt", "price(eur)", "plain"]
    quoted = {n: {s: quote(n, s) for s in SHELLS} for n in names}
    got = node_eval(f"setQuoting({json.dumps(quoted)}, {json.dumps(shell)}); "
                    f"cliCommand('approve', {json.dumps(names)})")
    assert got == command("approve", names, shell)


def test_dashboard_chip_without_a_safe_form_is_not_a_command():
    quoted = {"100%": {s: quote("100%", s) for s in SHELLS}}
    assert node_eval(f"setQuoting({json.dumps(quoted)}, 'cmd'); "
                     f"cliCommand('approve', ['100%'])") is None
    # a name the generator didn't quote is never pasted raw
    assert node_eval("setQuoting({}, 'posix'); cliCommand('approve', ['a;b'])") is None


def test_app_js_never_concatenates_raw_names_into_commands():
    js = APP_JS.read_text(encoding="utf-8")
    assert '"nightward approve " + it.name' not in js
    assert '"nightward reject " + it.name' not in js
    assert "i.name; }).join(" not in js


def test_cli_hints_quote_the_names_they_print(tmp_path):
    store = Store(tmp_path / ".tw")
    store.ensure()
    name = "x;touch${IFS}pwned1.txt"
    store.write_pending(Behavior(name=name, payload=list(range(200)), group="g"))
    store.approve(name)
    store.write_pending(Behavior(name=name, payload=[i * 7 + 1 for i in range(200)], group="g"))
    recompute(store)
    r = subprocess.run([sys.executable, "-m", "nightward", "review", "--dir", str(store.root)],
                       capture_output=True, text=True, encoding="utf-8", cwd=tmp_path)
    assert f"{command('review', [name])} --max-lines 0" in r.stdout.replace("\n", ""), r.stdout


# ---- R2-WEB-03: `approve NAME...` and a group chip that leaves REMOVED alone ---

def cli(*args, cwd):
    return subprocess.run([sys.executable, "-m", "nightward", *args], cwd=str(cwd),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def group_store(tw, removed_source="t.py::gone"):
    """gql.a/b/c CHANGED, gql.old REMOVED (its test didn't run), all in group graphql."""
    store = Store(tw)
    store.ensure()
    for n in ("gql.a", "gql.b", "gql.c", "gql.old"):
        src = removed_source if n == "gql.old" else "t.py::g"
        store.write_pending(Behavior(name=n, payload=1, group="graphql", source=src))
        store.approve(n)
    store.clear_pending()
    for n in ("gql.a", "gql.b", "gql.c"):
        store.write_pending(Behavior(name=n, payload=2, group="graphql", source="t.py::g"))
    store.write_run_meta({"completed": ["t.py::g"]})
    recompute(store)
    # approve only promotes a capture a human has seen (D10 / R2-WEB-01).
    assert cli("review", "--dir", str(tw), cwd=tw.parent).returncode == 0
    return store


def test_approve_accepts_several_names(tmp_path):
    store = group_store(tmp_path / ".tw")
    r = cli("approve", "gql.a", "gql.b", "--dir", str(store.root), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    base = store.load_baseline()
    assert base["gql.a"].payload == base["gql.b"].payload == 2
    assert base["gql.c"].payload == 1


def test_approve_with_an_unknown_name_approves_nothing(tmp_path):
    store = group_store(tmp_path / ".tw")
    r = cli("approve", "gql.a", "gql.zz", "--dir", str(store.root), cwd=tmp_path)
    assert r.returncode == 2
    assert "gql.zz" in r.stderr and "Traceback" not in r.stderr
    assert store.load_baseline()["gql.a"].payload == 1


def test_several_names_keep_unproven_removals_and_rejections(tmp_path):
    store = group_store(tmp_path / ".tw")
    assert cli("reject", "gql.c", "--dir", str(store.root), cwd=tmp_path).returncode == 0
    r = cli("approve", "gql.a", "gql.c", "gql.old", "--dir", str(store.root), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    base = store.load_baseline()
    assert base["gql.a"].payload == 2
    assert base["gql.c"].payload == 1 and "kept (rejected)" in r.stdout
    assert "gql.old" in base and "can't prove gone" in r.stdout


def test_one_name_still_overrides(tmp_path):
    store = group_store(tmp_path / ".tw")
    r = cli("approve", "gql.old", "--dir", str(store.root), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "gql.old" not in store.load_baseline()


def test_approve_names_and_all_together_is_an_error(tmp_path):
    store = group_store(tmp_path / ".tw")
    r = cli("approve", "gql.a", "gql.b", "--all", "--dir", str(store.root), cwd=tmp_path)
    assert r.returncode == 2


def test_group_chip_leaves_removed_items_out():
    items = [{"name": "gql.a", "kind": "CHANGED"}, {"name": "gql.old", "kind": "REMOVED"},
             {"name": "gql.n", "kind": "NEW"}]
    assert node_eval(f"groupApproveNames({json.dumps(items)})") == ["gql.a", "gql.n"]


# ---- R2-FIN-04: look-alike changes are made visible, never the verdict --------

NBSP, NNBSP, ZWSP = " ", " ", "​"


def diff_of(old, new):
    from nightward.core.diff import compare
    (change,) = compare({"p": Behavior(name="p", payload=old)},
                        {"p": Behavior(name="p", payload=new)})
    assert change.kind == "CHANGED"
    return change.diff_text


def test_nbsp_to_narrow_nbsp_is_escaped_and_named():
    d = diff_of({"pro": f"1{NBSP}234,56{NBSP}€"}, {"pro": f"1{NNBSP}234,56{NBSP}€"})
    lines = d.splitlines()
    minus = next(ln for ln in lines if ln.startswith("-  "))
    plus = next(ln for ln in lines if ln.startswith("+  "))
    assert r"1\u00a0234" in minus and r"1\u202f234" in plus
    assert f"56{NBSP}€" in plus          # only the differing character is escaped
    assert any(ln.startswith("? ") and "NARROW NO-BREAK SPACE" in ln for ln in lines), d


@pytest.mark.parametrize("old, new, word", [
    ("total", f"to{ZWSP}tal", "ZERO WIDTH SPACE"),
    ("paypal", "pаypal", "CYRILLIC SMALL LETTER A"),
    ("a b", "a  b", "SPACE"),
    ("a‎b", "ab", "LEFT-TO-RIGHT MARK"),
    ("line1\nline2", "line1 \nline2", "SPACE"),           # trailing space in a block
])
def test_invisible_and_confusable_changes_are_named(old, new, word):
    d = diff_of({"v": old}, {"v": new})
    assert any(ln.startswith("? ") and word in ln for ln in d.splitlines()), d


@pytest.mark.parametrize("old, new", [
    ({"v": "abc"}, {"v": "abd"}),
    ({"v": "가격"}, {"v": "나격"}),       # a visible Hangul change
    ({"a": {"b": 1}}, {"a": 1}),                           # structure / indentation
])
def test_visible_changes_are_not_annotated(old, new):
    d = diff_of(old, new)
    assert not any(ln.startswith("? ") for ln in d.splitlines()), d
    assert r"\u" not in d


def test_doctor_detail_shows_the_invisible_change():
    from nightward.core.doctor import diagnose
    diag = diagnose({"p": Behavior(name="p", payload={"pro": f"1{NBSP}234"})},
                    {"p": Behavior(name="p", payload={"pro": f"1{NNBSP}234"})})
    (f,) = diag["behaviors"]["p"]
    assert r"\u00a0" in f["detail"] and r"\u202f" in f["detail"], f
    assert "NARROW NO-BREAK SPACE" in f["note"], f


def test_review_shows_the_invisible_change(tmp_path):
    store = Store(tmp_path / ".tw")
    store.ensure()
    store.write_pending(Behavior(name="price.fr_FR", payload={"pro": f"1{NBSP}234"}))
    store.approve("price.fr_FR")
    store.write_pending(Behavior(name="price.fr_FR", payload={"pro": f"1{NNBSP}234"}))
    recompute(store)
    r = cli("review", "--dir", str(store.root), cwd=tmp_path)
    out = " ".join(r.stdout.split())   # rich wraps long lines at the console width
    assert r"\u202f" in out and "NARROW NO-BREAK SPACE" in out, r.stdout


def test_dashboard_styles_hint_lines():
    js = APP_JS.read_text(encoding="utf-8")
    assert 'line.startsWith("? ")' in js and "diff-hint" in js

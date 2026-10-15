"""Distribution surfaces: the GitHub Action and the Claude Code plugin.

They ship from this repo, so their contracts are pinned here: what the action
runs, that the plugin and its marketplace agree on names and version, and that
the plugin never gives an agent a way to approve.
"""
import json
import re
from pathlib import Path

import nightward

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "nightward"


# ---- GitHub Action (action.yml at the repo root, as Marketplace requires) ----

def _action() -> str:
    return (ROOT / "action.yml").read_text(encoding="utf-8")


def test_action_has_marketplace_metadata():
    text = _action()
    for key in ("name:", "description:", "author:", "branding:", "runs:"):
        assert re.search(rf"^{key}", text, re.M), key
    assert re.search(r"^\s+using: ['\"]?composite", text, re.M)


def test_action_gates_and_never_approves():
    text = _action()
    assert "nightward run" in text and "nightward gate" in text
    # It may tell the human to approve (job summary); it never runs approve/reject.
    assert not re.search(r"^\s*nightward (approve|reject)\b", text, re.M)


def test_action_installs_the_tagged_version_by_default():
    # `uses: LunenLabs/nightward@v0.3.2` must install nightward==0.3.2, not whatever
    # PyPI has today, so a pinned workflow stays reproducible.
    assert "github.action_ref" in _action()


def test_ci_exercises_the_action():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "uses: ./" in ci


# ---- Claude Code plugin + marketplace -----------------------------------------

def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_marketplace_lists_the_plugin_by_its_manifest_name():
    market = _json(ROOT / ".claude-plugin" / "marketplace.json")
    manifest = _json(PLUGIN / ".claude-plugin" / "plugin.json")
    assert market["name"] and market["owner"]["name"]
    (entry,) = [p for p in market["plugins"] if p["name"] == "nightward"]
    assert entry["source"] == "./plugins/nightward"
    assert manifest["name"] == entry["name"]


def test_plugin_version_matches_the_package():
    assert _json(PLUGIN / ".claude-plugin" / "plugin.json")["version"] == nightward.__version__


def test_plugin_mcp_server_runs_nightward_mcp():
    servers = _json(PLUGIN / ".mcp.json")["mcpServers"]
    (server,) = servers.values()
    assert server["command"] == "nightward" and server["args"][0] == "mcp"


def test_plugin_skills_have_frontmatter_and_never_approve():
    skills = sorted((PLUGIN / "skills").glob("*/SKILL.md"))
    assert skills
    for skill in skills:
        text = skill.read_text(encoding="utf-8")
        front = text.split("---")[1]
        assert re.search(r"^name: \S", front, re.M) and re.search(r"^description: \S", front, re.M)
        # An agent may tell the human to approve; it must never run it.
        assert not re.search(r"^\s*(\$ )?nightward (approve|reject)\b", text, re.M), skill

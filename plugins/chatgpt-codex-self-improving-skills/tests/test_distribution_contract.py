import json
import re
from pathlib import Path


PLUGIN_NAME = "chatgpt-codex-self-improving-skills"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_ROOT.parents[1]
MANIFEST_PATH = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
MARKETPLACE_PATH = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
LEGACY_MARKETPLACE_PATH = REPO_ROOT / ".claude-plugin" / "marketplace.json"
MCP_PATH = PLUGIN_ROOT / ".mcp.json"
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)(?:[-+][0-9A-Za-z.-]+)?$"
)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_modern_marketplace_points_to_the_codex_plugin():
    marketplace = load_json(MARKETPLACE_PATH)
    manifest = load_json(MANIFEST_PATH)
    matches = [
        plugin
        for plugin in marketplace["plugins"]
        if plugin["name"] == PLUGIN_NAME
    ]

    assert marketplace["name"] == "self-improving-skills"
    assert marketplace["interface"]["displayName"] == "Self-Improving Skills"
    assert len(matches) == 1

    entry = matches[0]
    assert entry["name"] == manifest["name"] == PLUGIN_ROOT.name
    assert entry["source"] == {
        "source": "local",
        "path": "./plugins/chatgpt-codex-self-improving-skills",
    }
    assert entry["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }
    assert entry["category"] == manifest["interface"]["category"] == "Engineering"
    assert "products" not in entry["policy"]
    assert "version" not in entry


def test_legacy_and_plugin_local_marketplaces_no_longer_distribute_codex_plugin():
    legacy_marketplace = load_json(LEGACY_MARKETPLACE_PATH)

    assert all(
        plugin["name"] != PLUGIN_NAME
        for plugin in legacy_marketplace["plugins"]
    )
    assert not (PLUGIN_ROOT / "marketplace.json").exists()


def test_manifest_uses_single_version_source_and_default_hook_discovery():
    manifest = load_json(MANIFEST_PATH)

    assert SEMVER_RE.fullmatch(manifest["version"])
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert "hooks" not in manifest
    assert manifest["author"]["name"]
    assert manifest["interface"]["displayName"]
    assert (PLUGIN_ROOT / "hooks" / "hooks.json").is_file()
    assert (PLUGIN_ROOT / ".mcp.json").is_file()


def test_mcp_forwards_documented_settings_but_not_plugin_data():
    config = load_json(MCP_PATH)["mcpServers"]["self-improving-skills"]
    forwarded = set(config["env_vars"])

    assert forwarded == {
        "CODEX_SELF_IMPROVE_MODE",
        "CODEX_SELF_IMPROVE_AUTO",
        "CODEX_SELF_IMPROVE_CODEX_BIN",
        "CODEX_SELF_IMPROVE_INTERVAL",
        "CODEX_SELF_IMPROVE_CURATE_INTERVAL_DAYS",
        "CODEX_SELF_IMPROVE_CURATE_MIN_SKILLS",
        "CODEX_SELF_IMPROVE_SKILL_ROOTS",
        "CODEX_SELF_IMPROVE_WRITE_ROOTS",
        "CODEX_SELF_IMPROVE_CREATE_ROOT",
    }
    assert "PLUGIN_DATA" not in forwarded


def test_hooks_use_distributed_windows_python_wrapper_and_no_async_handlers():
    hooks = load_json(PLUGIN_ROOT / "hooks" / "hooks.json")["hooks"]
    wrapper = PLUGIN_ROOT / "scripts" / "run_python.cmd"
    scripts = {
        "SessionStart": "session_start.py",
        "PostToolUse": "post_tool_use.py",
        "Stop": "stop_review.py",
    }

    assert wrapper.is_file()

    for event, script in scripts.items():
        handler = hooks[event][0]["hooks"][0]
        assert handler["type"] == "command"
        assert handler["command"].startswith("python3 ")
        assert handler["commandWindows"] == (
            '"%PLUGIN_ROOT%\\scripts\\run_python.cmd" '
            f'"%PLUGIN_ROOT%\\scripts\\{script}"'
        )
        assert "async" not in handler


def test_windows_python_wrapper_prefers_launcher_and_preserves_exit_codes():
    wrapper = (PLUGIN_ROOT / "scripts" / "run_python.cmd").read_text(
        encoding="utf-8"
    )
    normalized = wrapper.replace("\r\n", "\n").lower()

    assert "where.exe py >nul 2>&1" in normalized
    assert "py -3 %*\nexit /b %errorlevel%" in normalized
    assert normalized.index("py -3 %*") < normalized.index(":python_fallback")
    assert "where.exe python >nul 2>&1" in normalized
    assert "python %*\nexit /b %errorlevel%" in normalized
    assert "exit /b 9009" in normalized

import json
import re
from pathlib import Path

import yaml


PLUGIN_NAME = "chatgpt-work-self-improving-skills"
SKILL_NAME = "work-self-improvement-review"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_ROOT.parents[1]
MANIFEST_PATH = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
MARKETPLACE_PATH = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
PLUGIN_LOCAL_MARKETPLACE_PATH = (
    PLUGIN_ROOT / ".agents" / "plugins" / "marketplace.json"
)
LEGACY_WORK_MARKETPLACE_PATH = (
    REPO_ROOT / "chatgpt-work" / ".agents" / "plugins" / "marketplace.json"
)
LEGACY_PLUGIN_ROOT = (
    REPO_ROOT / "chatgpt-work" / "plugins" / "chatgpt-work-self-improving-skills"
)
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)(?:[-+][0-9A-Za-z.-]+)?$"
)
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_skill(path: Path):
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---")
    parts = text.split("---", 2)
    assert len(parts) == 3
    return yaml.safe_load(parts[1]), parts[2].strip()


def test_manifest_is_skills_only_and_work_ready():
    manifest = load_json(MANIFEST_PATH)

    assert manifest["name"] == PLUGIN_NAME
    assert SEMVER_RE.fullmatch(manifest["version"])
    assert manifest["skills"] == "./skills/"
    assert manifest["author"]["name"]
    assert manifest["interface"]["displayName"]
    assert manifest["interface"]["category"]
    assert manifest["interface"]["capabilities"] == ["Interactive"]
    assert len(manifest["interface"]["defaultPrompt"]) >= 3

    for forbidden_key in ("hooks", "mcpServers", "apps", "dependencies"):
        assert forbidden_key not in manifest

    for forbidden_path in (
        PLUGIN_ROOT / "hooks",
        PLUGIN_ROOT / "scripts",
        PLUGIN_ROOT / ".mcp.json",
        PLUGIN_ROOT / ".app.json",
    ):
        assert not forbidden_path.exists()


def test_repo_marketplace_exposes_one_work_entry_without_product_gating():
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
        "path": "./plugins/chatgpt-work-self-improving-skills",
    }
    assert entry["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }
    assert entry["category"] == manifest["interface"]["category"] == "Productivity"
    assert "products" not in entry["policy"]
    assert "version" not in entry
    assert not PLUGIN_LOCAL_MARKETPLACE_PATH.exists()
    assert not LEGACY_WORK_MARKETPLACE_PATH.exists()
    assert not LEGACY_PLUGIN_ROOT.exists()


def test_skill_metadata_and_work_invocation_contract():
    skill_root = PLUGIN_ROOT / "skills" / SKILL_NAME
    frontmatter, body = load_skill(skill_root / "SKILL.md")
    openai = yaml.safe_load(
        (skill_root / "agents" / "openai.yaml").read_text(encoding="utf-8")
    )

    assert set(frontmatter) == {"name", "description"}
    assert frontmatter["name"] == skill_root.name == SKILL_NAME
    assert NAME_RE.fullmatch(frontmatter["name"])
    assert len(frontmatter["name"]) <= 64
    assert 0 < len(frontmatter["description"]) <= 1024
    assert "<" not in frontmatter["description"]
    assert ">" not in frontmatter["description"]
    assert body

    interface = openai["interface"]
    assert interface["display_name"]
    assert interface["short_description"]
    assert "@" + SKILL_NAME in interface["default_prompt"]
    assert "$" + SKILL_NAME not in interface["default_prompt"]
    assert openai["policy"]["allow_implicit_invocation"] is True
    assert "dependencies" not in openai


def test_runtime_files_do_not_reference_local_only_mechanisms():
    runtime_paths = [
        MANIFEST_PATH,
        PLUGIN_ROOT / "skills" / SKILL_NAME / "SKILL.md",
        PLUGIN_ROOT / "skills" / SKILL_NAME / "agents" / "openai.yaml",
    ]
    combined = chr(10).join(
        path.read_text(encoding="utf-8") for path in runtime_paths
    )
    forbidden_tokens = (
        "~/.codex",
        "~/.claude",
        "/Users/",
        "CODEX_HOME",
        "PLUGIN_DATA",
        "transcript_path",
        "SendUserFile",
        "codex_skill_",
        "skill_manager",
        "subprocess",
        "python3",
        "STDIO",
        "claude-cowork-self-improving-skills",
        "claude.ai",
    )

    for token in forbidden_tokens:
        assert token not in combined


def test_review_contract_requires_evidence_approval_and_safe_export():
    skill_text = (
        PLUGIN_ROOT / "skills" / SKILL_NAME / "SKILL.md"
    ).read_text(encoding="utf-8")
    required_phrases = (
        "Use only the current conversation",
        "Do not treat invocation of this skill as approval",
        "Complete proposal and approval in separate turns",
        "status: pending",
        "status: no-change",
        "new SKILL candidate",
        "Reject the candidate when safe generalization is impossible",
        "Never claim the candidate is installed or published",
    )

    for phrase in required_phrases:
        assert phrase in skill_text

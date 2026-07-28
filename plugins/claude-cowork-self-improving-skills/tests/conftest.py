"""Shared fixtures for the claude-cowork-self-improving-skills test suite.

Every test runs against a sandboxed HOME (tmp_path) so the real
~/.claude/skills and ~/.claude/self-improve are never touched:

  - hook-contract tests invoke the scripts as subprocesses with HOME swapped
    (the same way Claude Code runs them: JSON on stdin, JSON/empty on stdout);
  - unit tests import the modules and reload them so their module-level
    expanduser() paths re-resolve into the sandbox.
"""

import importlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, SCRIPTS_DIR)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Sandboxed HOME + freshly-reloaded modules bound to it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Pin the runtime these tests describe. Every hook here now stands down
    # outside Cowork, and a tmp_path home detects as LOCAL (it is nowhere near
    # a local-agent-mode-sessions path), so without this pin every hook contract
    # below would assert against a hook that returned early. runtime_env's own
    # detection is tested in test_runtime_env.py, which clears this. Set via
    # monkeypatch so the subprocess runs inherit it through os.environ.
    monkeypatch.setenv("SIS_RUNTIME", "cowork")
    import usage_store
    importlib.reload(usage_store)

    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)

    def make_skill(name, body=None):
        d = skills / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            body or "---\nname: {0}\ndescription: d\n---\nbody\n".format(name),
            encoding="utf-8")
        return d

    return SimpleNamespace(
        home=tmp_path,
        skills=skills,
        make_skill=make_skill,
        usage_store=usage_store,
    )


def _run_script(home, script, payload):
    env = dict(os.environ, HOME=str(home))
    p = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, script)],
        input=json.dumps(payload), capture_output=True, text=True, env=env)
    return p.stdout


def tool_use(name, inp):
    """A transcript row in the REAL shape (assistant row with tool_use block)."""
    return {"type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


@pytest.fixture
def run_analyzer(sandbox):
    """Run analyze_turn.py against a fixture transcript; returns its decision."""
    def _run(rows, sid, extra=None):
        tp = sandbox.home / "{0}.jsonl".format(sid)
        tp.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        payload = {"transcript_path": str(tp), "session_id": sid}
        payload.update(extra or {})
        return json.loads(_run_script(sandbox.home, "analyze_turn.py", payload))
    return _run


@pytest.fixture
def run_validator(sandbox):
    """Run validate_skill.py with a PostToolUse payload; returns raw stdout."""
    def _run(payload):
        return _run_script(sandbox.home, "validate_skill.py", payload)
    return _run


@pytest.fixture
def run_advisory(sandbox):
    """Run session_advisory.py (UserPromptSubmit hook); returns raw stdout."""
    def _run():
        return _run_script(sandbox.home, "session_advisory.py", {})
    return _run


@pytest.fixture
def mark_advisory_shown(sandbox):
    """Pre-mark the once-per-session advisory flag (as the UserPromptSubmit
    hook would have) so analyzer tests exercise the no-fallback path."""
    def _mark():
        d = sandbox.home / ".claude" / "self-improve"
        d.mkdir(parents=True, exist_ok=True)
        (d / "advisory_shown").write_text("shown\n", encoding="utf-8")
    return _mark


@pytest.fixture
def store_data(sandbox):
    """Read the sandboxed skill_usage.json."""
    def _read():
        path = sandbox.home / ".claude" / "self-improve" / "skill_usage.json"
        return json.loads(path.read_text(encoding="utf-8"))
    return _read

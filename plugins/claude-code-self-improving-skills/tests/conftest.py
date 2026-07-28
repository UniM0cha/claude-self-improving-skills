"""Shared fixtures for the claude-code-self-improving-skills test suite.

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


def _sandbox_home_env(home):
    """The env vars that redirect a home directory on every supported OS.

    HOME alone is not enough: ntpath.expanduser consults USERPROFILE (then
    HOMEDRIVE+HOMEPATH) and ignores HOME entirely, so a HOME-only sandbox
    silently writes into the developer's REAL ~/.claude on Windows.
    """
    drive, tail = os.path.splitdrive(str(home))
    return {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "HOMEDRIVE": drive,
        "HOMEPATH": tail or os.sep,
    }


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Sandboxed HOME + freshly-reloaded modules bound to it."""
    for key, value in _sandbox_home_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    # Pin the runtime these tests describe. Every hook here now stands down in
    # Cowork, so the contracts below only mean anything in a local runtime.
    # A tmp_path home already detects as local, but pinning keeps these tests
    # describing one runtime on purpose rather than by accident of where pytest
    # put its temp dir. runtime_env's own detection is tested in
    # test_runtime_env.py, which clears this. Set via monkeypatch so the
    # subprocess runs inherit it through os.environ.
    monkeypatch.setenv("SIS_RUNTIME", "local")
    import curator_backup
    import curator_transitions
    import usage_store
    importlib.reload(usage_store)
    importlib.reload(curator_backup)
    importlib.reload(curator_transitions)

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
        curator=curator_transitions,
    )


def _run_script(home, script, payload, extra_env=None):
    env = dict(os.environ, **_sandbox_home_env(home))
    env.update(extra_env or {})
    p = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, script)],
        # ensure_ascii=False so a non-ASCII path/message reaches the hook as raw
        # UTF-8 bytes, exactly as Claude Code (Node JSON.stringify) delivers it —
        # \u-escaping it here would hide the very decode path a Windows codepage
        # breaks. Decode the hook's stdout as UTF-8 too, matching what the
        # scripts now pin, not the parent's locale codec (cp1252 on Windows).
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True, text=True, encoding="utf-8", env=env)
    return p.stdout


def tool_use(name, inp):
    """A transcript row in the REAL shape (assistant row with tool_use block)."""
    return {"type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


@pytest.fixture
def run_analyzer(sandbox):
    """Run analyze_turn.py against a fixture transcript; returns its decision.

    Defaults to foreground mode so these tests exercise the nudge contract
    directly. Background mode is covered by its own tests, which pass
    `env={"SIS_REVIEW_MODE": "background"}` explicitly.
    """
    def _run(rows, sid, extra=None, env=None):
        tp = sandbox.home / "{0}.jsonl".format(sid)
        tp.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        payload = {"transcript_path": str(tp), "session_id": sid}
        payload.update(extra or {})
        run_env = {"SIS_REVIEW_MODE": "foreground"}
        run_env.update(env or {})
        return json.loads(
            _run_script(sandbox.home, "analyze_turn.py", payload, run_env))
    return _run


@pytest.fixture
def run_validator(sandbox):
    """Run validate_skill.py with a PostToolUse payload; returns raw stdout."""
    def _run(payload):
        return _run_script(sandbox.home, "validate_skill.py", payload)
    return _run


@pytest.fixture
def store_data(sandbox):
    """Read the sandboxed skill_usage.json."""
    def _read():
        path = sandbox.home / ".claude" / "self-improve" / "skill_usage.json"
        return json.loads(path.read_text(encoding="utf-8"))
    return _read

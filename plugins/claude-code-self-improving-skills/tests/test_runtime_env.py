"""Which variant may act in a session, and proof this one stands down elsewhere.

Both self-improving-skills variants register the same hook events. A machine
that can reach both — a local install plus the Cowork variant synced from the
claude.ai account — would otherwise run both on one Stop and answer twice, once
telling the user the skill is already on disk and once telling them to press
"스킬 저장".

The conftest pins SIS_RUNTIME=local for every other test; the `unpinned`
fixture removes that pin so the detection itself can be exercised.
"""

import pytest
from conftest import _sandbox_home_env, tool_use

import runtime_env


@pytest.fixture
def unpinned(sandbox, monkeypatch):
    """The sandbox with conftest's SIS_RUNTIME pin removed."""
    monkeypatch.delenv("SIS_RUNTIME", raising=False)
    return sandbox


def _swap_home(monkeypatch, home):
    for key, value in _sandbox_home_env(home).items():
        monkeypatch.setenv(key, value)


def _mark_cli_install(home):
    """Write the file the CLI installer leaves behind on a local install.

    Detection must NOT depend on it — see the regression test below — but a
    machine that has it is still local, so both states are covered.
    """
    d = home / ".claude" / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    (d / "installed_plugins.json").write_text("{}\n", encoding="utf-8")


# --- explicit override ------------------------------------------------------

def test_override_wins_over_a_local_home(unpinned, monkeypatch):
    monkeypatch.setenv("SIS_RUNTIME", "cowork")
    assert runtime_env.is_cowork_runtime() is True


def test_override_wins_over_a_sandbox_home(unpinned, monkeypatch, tmp_path):
    home = tmp_path / runtime_env.SANDBOX_SEGMENT / "acct" / "org" / "local_abc"
    home.mkdir(parents=True)
    _swap_home(monkeypatch, home)
    monkeypatch.setenv("SIS_RUNTIME", "local")
    assert runtime_env.is_local_runtime() is True


def test_an_unusable_override_falls_back_to_detection(unpinned, monkeypatch):
    monkeypatch.setenv("SIS_RUNTIME", "yes-please")
    assert runtime_env.is_cowork_runtime() is False


# --- detection --------------------------------------------------------------

def test_a_sandbox_home_path_reads_as_cowork(unpinned, monkeypatch, tmp_path):
    home = tmp_path / runtime_env.SANDBOX_SEGMENT / "acct" / "org" / "local_abc"
    home.mkdir(parents=True)
    _swap_home(monkeypatch, home)
    assert runtime_env.is_cowork_runtime() is True


def test_a_local_home_without_the_cli_marker_is_still_local(unpinned):
    """Regression: absence of installed_plugins.json must not mean Cowork.

    Plugins synced from the claude.ai account never land in that file, so a
    user who installs everything through the account and nothing through the
    CLI has an ordinary local machine with no such file. Reading that as Cowork
    made this variant stand down and silently took the whole loop with it.
    """
    marker = unpinned.home / ".claude" / "plugins" / "installed_plugins.json"
    assert not marker.exists()
    assert runtime_env.is_cowork_runtime() is False


def test_a_local_home_with_the_cli_marker_is_local(unpinned):
    _mark_cli_install(unpinned.home)
    assert runtime_env.is_cowork_runtime() is False


def test_the_segment_must_match_a_whole_path_component(unpinned, monkeypatch,
                                                       tmp_path):
    # A directory that merely CONTAINS the segment name is an ordinary home;
    # matching on substrings would strand it in the wrong runtime.
    home = tmp_path / (runtime_env.SANDBOX_SEGMENT + "-backup")
    home.mkdir()
    _swap_home(monkeypatch, home)
    assert runtime_env.is_cowork_runtime() is False


def test_home_is_re_read_on_every_call(unpinned, monkeypatch, tmp_path):
    # A home cached at import time would keep answering for the first home the
    # process ever saw — the trap skill_paths documents.
    local = tmp_path / "local_home"
    local.mkdir()
    _swap_home(monkeypatch, local)
    assert runtime_env.is_cowork_runtime() is False

    sandbox_home = tmp_path / runtime_env.SANDBOX_SEGMENT / "acct" / "local_x"
    sandbox_home.mkdir(parents=True)
    _swap_home(monkeypatch, sandbox_home)
    assert runtime_env.is_cowork_runtime() is True


# --- hook contract ----------------------------------------------------------

def _work_rows(calls=12, edits=2):
    rows = [tool_use("Bash", {"command": "x"}) for _ in range(calls)]
    rows += [tool_use("Edit", {"file_path": "/tmp/f{0}.py".format(i)})
             for i in range(edits)]
    return rows


def test_the_stop_hook_stands_down_in_cowork(run_analyzer):
    """Work that nudges locally must leave a bare approve inside Cowork."""
    r = run_analyzer(_work_rows(), "s", env={"SIS_RUNTIME": "cowork"})
    assert r == {"decision": "approve"}


def test_the_stop_hook_still_fires_locally(run_analyzer):
    """The gate must not swallow the local case it is meant to protect."""
    r = run_analyzer(_work_rows(), "s")
    assert r["decision"] == "block"

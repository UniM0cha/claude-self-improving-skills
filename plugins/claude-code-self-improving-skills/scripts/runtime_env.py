#!/usr/bin/env python3
"""Tells a hook whether it runs under local Claude Code or inside Cowork.

Both self-improving-skills variants register the same hook events, and exactly
one of them should act in any given session. The local variant patches
``~/.claude/skills`` in place because that directory outlives the session; the
Cowork variant has to route every skill back through claude.ai because a Cowork
session's home is discarded at session end. With both reachable and no gate, a
single Stop runs both and the user gets the wrong workflow — the Cowork nudge
telling them to press "스킬 저장" on a machine where the skill was already
written to disk.

HOME is read on every call, never cached at import time: the tests swap HOME
after import, and a cached answer would describe the wrong home. That is the
same trap :mod:`skill_paths` documents.

Detection rests on one positive signal: HOME sits under a
``local-agent-mode-sessions`` path segment, which is where Cowork puts each
session's sandbox home when it runs on the user's own machine.

An earlier revision also read a missing ``~/.claude/plugins/installed_plugins.json``
as Cowork, reasoning that only the CLI installer writes it. That is wrong in a
way that fails silently: plugins synced from the claude.ai account never appear
in that file, so someone who installs everything through the account and nothing
through the CLI has no such file on a perfectly ordinary local machine. The
local variant would stand down there and take the whole distillation loop with
it, with nothing printed — hook wrappers discard stderr, and the variant that
would have announced the loop is the one that just went silent. Absence of a
file is not evidence of an environment.

Dropping that signal costs the reverse case: a Cowork container whose home is
nowhere near the sandbox path now reads as local. That environment has not been
observed from here, and its failure mode — a skill distilled into a home that is
later discarded — is milder than silently disabling the loop on a real local
machine. Pin ``SIS_RUNTIME`` there, and add a positive signal once such an
environment is actually observed rather than guessed at.

``SIS_RUNTIME`` (``local`` | ``cowork``) overrides detection entirely.
"""

import os

SANDBOX_SEGMENT = "local-agent-mode-sessions"


def _home():
    return os.path.realpath(os.path.expanduser("~"))


def runtime_override():
    """Return the pinned runtime name, or None when SIS_RUNTIME is unusable."""
    value = (os.environ.get("SIS_RUNTIME") or "").strip().lower()
    return value if value in ("local", "cowork") else None


def is_cowork_runtime():
    """True when this hook is running inside a Cowork session."""
    override = runtime_override()
    if override is not None:
        return override == "cowork"

    # Whole path components only: a directory merely containing this name
    # (…/local-agent-mode-sessions-backup) is an ordinary home.
    return SANDBOX_SEGMENT in _home().split(os.sep)


def is_local_runtime():
    """True when this hook is running under a local Claude Code session."""
    return not is_cowork_runtime()

#!/usr/bin/env python3
"""PreToolUse hook: back up a learned SKILL.md right BEFORE it is edited.

Pairs with validate_skill.py (PostToolUse): if the edit breaks the frontmatter,
the validator restores this backup — giving the distiller's in-place patches the
same transactional safety as Hermes _patch_skill (backup → re-validate → rollback).

Behavior:
  - Only acts on a Write/Edit/MultiEdit whose file_path is ~/.claude/skills/**/SKILL.md.
  - Existing file  -> copy it to the skill's backup path (the rollback source).
  - New file       -> remove any stale backup (nothing to roll back to).
Silent and fail-safe: never blocks the edit.

The backup location comes from skill_paths.backup_path so this hook and the
validator that rolls back cannot drift apart.
"""

import json
import os
import shutil
import sys

import runtime_env
import sis_io
# Plain import: Python puts a script's own directory on sys.path[0], and the
# test suite adds scripts/ explicitly, so no path bootstrap is needed here.
from skill_paths import backup_dir, backup_path, is_learned_skill

# Pin UTF-8 before reading stdin: a Korean file path in the payload would
# otherwise crash the locale-codec decode, and this hook swallows that and
# exits WITHOUT the pre-edit backup, leaving the validator nothing to roll a
# broken edit back to.
sis_io.pin_utf8_stdio()


def main():
    # The cowork variant backs the same file up inside Cowork, into the same
    # directory. Running both would just write the copy twice.
    if runtime_env.is_cowork_runtime():
        sys.exit(0)

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        sys.exit(0)
    raw_inp = payload.get("tool_input")
    inp = raw_inp if isinstance(raw_inp, dict) else {}
    fp = inp.get("file_path", "")
    if not is_learned_skill(fp):
        sys.exit(0)
    try:
        os.makedirs(backup_dir(), exist_ok=True)
        bp = backup_path(fp)
        if os.path.isfile(fp):
            shutil.copy2(fp, bp)          # existing -> rollback source
        elif os.path.isfile(bp):
            os.unlink(bp)                 # new file -> drop stale backup
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""PreToolUse hook: back up a learned SKILL.md right BEFORE it is edited.

Pairs with validate_skill.py (PostToolUse): if the edit breaks the frontmatter,
the validator restores this backup — giving the distiller's in-place patches the
same transactional safety as Hermes _patch_skill (backup → re-validate → rollback).

Behavior:
  - Only acts on a Write/Edit/MultiEdit whose file_path is ~/.claude/skills/**/SKILL.md.
  - Existing file  -> copy it to <backup_dir>/<name>.bak (the rollback source).
  - New file       -> remove any stale <name>.bak (nothing to roll back to).
Silent and fail-safe: never blocks the edit.
"""

import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runtime_env

BACKUP_DIR = os.path.expanduser("~/.claude/self-improve/skill_backups")


def _is_learned_skill(file_path):
    norm = str(file_path or "").replace("\\", "/")
    return "/.claude/skills/" in norm and norm.endswith("SKILL.md")


def _backup_path(file_path):
    norm = str(file_path).replace("\\", "/")
    name = os.path.basename(os.path.dirname(norm))
    return os.path.join(BACKUP_DIR, name + ".bak")


def main():
    # The claude-code variant backs the same file up on a local machine, into
    # the same directory. Running both would just write the copy twice.
    if runtime_env.is_local_runtime():
        sys.exit(0)

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        sys.exit(0)
    raw_inp = payload.get("tool_input")
    inp = raw_inp if isinstance(raw_inp, dict) else {}
    fp = inp.get("file_path", "")
    if not _is_learned_skill(fp):
        sys.exit(0)
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        bp = _backup_path(fp)
        if os.path.isfile(fp):
            shutil.copy2(fp, bp)          # existing -> rollback source
        elif os.path.isfile(bp):
            os.unlink(bp)                 # new file -> drop stale backup
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()

#!/bin/bash
# PreToolUse hook (Write|Edit|MultiEdit) — back up a learned SKILL.md before edit
# so validate-skill-cowork.sh can roll back a structure-breaking change. Silent; never
# blocks the edit (fail-safe).
set -uo pipefail

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/backup_skill.py" 2>/dev/null || true
exit 0

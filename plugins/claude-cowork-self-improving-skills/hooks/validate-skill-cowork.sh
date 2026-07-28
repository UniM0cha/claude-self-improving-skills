#!/bin/bash
# PostToolUse hook (Write|Edit|MultiEdit) — validate & provenance-stamp learned
# SKILL.md files. Delegates to validate_skill.py. Silent for non-skill edits and
# fails safe to silent on any error.
set -uo pipefail

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/validate_skill.py" 2>/dev/null || true
exit 0

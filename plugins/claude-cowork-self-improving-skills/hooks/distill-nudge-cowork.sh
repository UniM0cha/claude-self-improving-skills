#!/bin/bash
# Stop hook — nudge the agent to distil reusable skills after a complex segment.
# Delegates all logic to analyze_turn.py. Fails safe to {"decision":"approve"}
# on ANY error so a broken analyzer can never wedge a session shut.
set -uo pipefail

out=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/analyze_turn.py" 2>/dev/null) || {
  echo '{"decision":"approve"}'
  exit 0
}
if [ -z "$out" ]; then
  echo '{"decision":"approve"}'
  exit 0
fi
printf '%s\n' "$out"
exit 0

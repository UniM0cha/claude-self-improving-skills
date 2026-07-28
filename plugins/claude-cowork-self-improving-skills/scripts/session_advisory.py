#!/usr/bin/env python3
"""UserPromptSubmit-hook logic for the claude-cowork-self-improving-skills plugin.

Injects the self-improvement advisory ONCE per session, on the FIRST user
prompt — the Cowork replacement for the original plugin's SessionStart hook.

Why not SessionStart: on a cold Cowork container the runtime loads plugin
hooks BEFORE the plugin download (`plugins_sync_complete`) finishes, so the
SessionStart EVENT fires while this plugin's hooks.json does not exist on
disk yet — the advisory is silently lost every cold start (verified against
the startup diagnostics timeline, 2026-07-16). The first user prompt, by
contrast, is only processed after the runtime has waited for plugin sync
(`plugins_sync_wait`), so a UserPromptSubmit hook fires reliably.

Once-per-session guard: an advisory flag file under ~/.claude/self-improve/.
Cowork containers are per-session, so the flag naturally resets each session.
The Stop-hook analyzer (analyze_turn.py) uses the same flag as a fallback: if
a nudge fires before this hook ever ran, it prepends a short advisory itself.

Output contract: a UserPromptSubmit hook adds context by printing
  {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                          "additionalContext": "..."}}
to stdout with exit 0. Fails safe to silent (no context) on any error.
"""

import json
import os
import sys
from typing import NoReturn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runtime_env

SKILLS_DIR = os.path.expanduser("~/.claude/skills")
STATE_DIR = os.path.expanduser("~/.claude/self-improve")
ADVISORY_FLAG = os.path.join(STATE_DIR, "advisory_shown")
PROVENANCE_KEY = "self-improving-skills"  # provenance marker (shared with the
# original plugin on purpose: skills saved to claude.ai from Cowork keep
# working with the local-CLI plugin's counter/curator, and vice versa)


def emit_context(text) -> NoReturn:
    if text:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": text,
            }
        }, ensure_ascii=False))
    sys.exit(0)


def _count_learned_skills():
    """Count learned skills: DIRECT children of ~/.claude/skills whose SKILL.md
    carries the provenance marker (same counting rule as the original plugin —
    support-dir SKILL.md copies under references/ etc. must not inflate it)."""
    learned = 0
    if not os.path.isdir(SKILLS_DIR):
        return learned
    try:
        entries = os.listdir(SKILLS_DIR)
    except Exception:
        return learned
    for entry in entries:
        if entry.startswith("."):
            continue
        path = os.path.join(SKILLS_DIR, entry, "SKILL.md")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                head = fh.read(2048)
            # either explicit provenance or an origin marker counts (a distiller
            # may write its own metadata block with only origin: distilled,
            # which the validator then leaves untouched)
            if PROVENANCE_KEY in head or "origin: distilled" in head:
                learned += 1
        except Exception:
            pass
    return learned


def _mark_shown():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(ADVISORY_FLAG, "w", encoding="utf-8") as fh:
            fh.write("shown\n")
    except Exception:
        pass


def main():
    # A local session gets the claude-code variant's SessionStart notice
    # instead; announcing the Cowork loop there would describe a save flow the
    # user never has to perform.
    if runtime_env.is_local_runtime():
        emit_context(None)

    try:
        sys.stdin.read()  # drain payload; we don't need its fields
    except Exception:
        pass

    # Once per session: Cowork containers are per-session, so a plain flag
    # file is exactly per-session state.
    if os.path.isfile(ADVISORY_FLAG):
        emit_context(None)
    _mark_shown()

    try:
        learned = _count_learned_skills()
    except Exception:
        learned = 0

    lines = [
        "[claude-cowork-self-improving-skills] 자기개선 루프 활성. 복잡한 작업·까다로운 디버깅·"
        "새 기법 발견을 끝냈고 재사용 가능하다면, /distill-skill 을 실행하거나 Task(또는 "
        "Agent) 도구로 subagent_type=\"claude-cowork-self-improving-skills:skill-distiller\""
        "(플러그인 네임스페이스 접두사 생략 시 호출 실패)를 호출해 ~/.claude/skills 에 "
        "학습 스킬로 남기세요 — 그냥 종료하면 Stop 훅이 한 번 상기시킵니다.",
        "Cowork 핵심 규칙 — 이 컨테이너의 ~/.claude/skills 는 세션이 끝나면 사라집니다. "
        "스킬을 증류하거나 patch 했다면 반드시 그 SKILL.md 를 SendUserFile 로 사용자에게 "
        "보내고, 파일 카드의 '스킬 저장' 버튼으로 claude.ai 에 등록하도록 안내하세요. "
        "등록된 스킬만 다음 세션 컨테이너에 자동 동기화됩니다 (/save-skill 이 이 흐름을 "
        "대신 처리합니다).",
        "스킬 name 에는 'claude'/'anthropic' 을(예약어), description 에는 꺾쇠 태그 "
        "형태(<...> placeholder 포함)를 쓰지 마세요 — claude.ai '스킬 저장'이 거부합니다. "
        "미저장 스킬은 세션 도중에도 주기적 스킬 동기화가 삭제할 수 있으니(실측), "
        "증류 직후 바로 전송·저장을 안내하세요.",
        "학습 스킬에서 낡거나 틀린 내용을 발견하면 그 자리에서 SKILL.md 를 patch 하고, "
        "patch 한 스킬도 다시 저장(SendUserFile → '스킬 저장')하도록 안내하세요.",
    ]
    if learned:
        lines.append("현재 이 컨테이너에 학습 스킬 {0}개가 동기화되어 있습니다 "
                     "(/loop-status 로 저장 여부 확인).".format(learned))

    emit_context("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Fail-safe: never break prompt processing.
        sys.exit(0)

#!/usr/bin/env python3
"""SessionStart-hook logic for the claude-code-self-improving-skills plugin.

Injects a small amount of additionalContext at session start so the agent:
  1. knows the self-improvement loop is active and how to feed it (the advisory
     nudge — Hermes' SKILLS_GUIDANCE analogue),
  2. is aware of how many learned skills already exist under ~/.claude/skills, and
  3. is reminded to run /curate-skills when the learned-skill library has grown
     and hasn't been consolidated in a while (the Hermes 7-day curator analogue,
     here event-gated rather than wall-clock).

Output contract: a SessionStart hook adds context by printing
  {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}
to stdout. Fails safe to silent (no context) on any error.

Config:
  SIS_CURATE_MIN_SKILLS  learned-skill count above which curation is suggested (default 8)
  SIS_CURATE_INTERVAL_DAYS  days since last curation before re-suggesting (default 7)
"""

import json
import os
import sys
import time
from typing import NoReturn

import runtime_env
import sis_io
import skill_paths

# Pin UTF-8 before the (Korean) SessionStart note is written; see sis_io.
sis_io.pin_utf8_stdio()

SKILLS_DIR = skill_paths.personal_skills_root()
# Resolved through skill_paths so SIS_STATE_DIR moves ALL plugin state; a
# hard-coded default here would leave this file behind in the real home.
STATE_DIR = skill_paths.state_dir()
CURATOR_STATE = os.path.join(STATE_DIR, "curator_state.json")
# Kept out of curator_state.json: the curator seeds that file with a fresh
# dict on first run, which would wipe whatever we stored alongside it.
DISTILL_SEEN = os.path.join(STATE_DIR, "distill_seen.json")
PROVENANCE_KEY = "self-improving-skills"  # marker we write into learned SKILL.md frontmatter


def emit_context(text, reload_skills=False) -> NoReturn:
    if text:
        payload = {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
        if reload_skills:
            # Skill discovery runs before SessionStart hooks finish, so a skill
            # the background worker installed since the last session would
            # otherwise only become available one session later.
            payload["reloadSkills"] = True
        sys.stdout.write(json.dumps({"hookSpecificOutput": payload}, ensure_ascii=False))
    sys.exit(0)


def _review_mode():
    mode = (os.environ.get("SIS_REVIEW_MODE") or "").strip().lower()
    return mode if mode in ("background", "foreground", "off") else "background"


def _background_note():
    """(note, reload_skills) for the background queue.

    Silence is the default: a job that distilled something, or decided there
    was nothing worth keeping, is not news. Only states a human has to act on
    are surfaced — plus a relaunch when work is waiting and no worker is alive,
    which is how a job survives a machine restart.
    """
    try:
        import distill_queue
        import distill_worker
    except Exception:
        return None, False

    try:
        queue = distill_queue.DistillQueue()
        counts = queue.status()["counts"]
    except Exception:
        return None, False

    seen = _read_seen()
    alerts = []
    reload_skills = False

    try:
        done = queue.list_jobs(status="done", limit=50)
    except Exception:
        done = []
    fresh = [job for job in done if str(job["id"]) not in seen]
    if any((job.get("result") or {}).get("skills") for job in fresh):
        reload_skills = True

    blocked_by_code = {}
    try:
        for job in queue.list_jobs(status="blocked", limit=100):
            blocked_by_code.setdefault(job.get("error_code") or "unknown", 0)
            blocked_by_code[job.get("error_code") or "unknown"] += 1
    except Exception:
        blocked_by_code = {}

    if blocked_by_code.get("authentication_required"):
        alerts.append(
            "증류 작업 {0}건이 CLI 인증을 기다립니다 — `claude setup-token` 후 "
            "{1} 에 CLAUDE_CODE_OAUTH_TOKEN 을 넣고 /distill-status retry 로 "
            "재시도하세요".format(
                blocked_by_code["authentication_required"],
                distill_worker.worker_env_file()))
    if blocked_by_code.get("unprotected_write"):
        alerts.append(
            "증류 작업 {0}건에서 되돌릴 수 없는 쓰기가 감지돼 보류됐습니다 — "
            "/distill-status 로 경로를 확인하세요".format(
                blocked_by_code["unprotected_write"]))
    if blocked_by_code.get("symlinked_skills"):
        # No longer produced: the symlink refusal was removed. Jobs blocked by
        # the old rule are still sitting in queues out there, and they retry
        # cleanly now, so point at that instead of restating a rule that is gone.
        alerts.append(
            "심볼릭 링크 때문에 보류됐던 증류 작업 {0}건이 있습니다 — 이 제한은 "
            "없어졌으니 /distill-status retry 로 재시도하세요".format(
                blocked_by_code["symlinked_skills"]))
    other_blocked = sum(
        count for code, count in blocked_by_code.items()
        if code not in ("authentication_required", "unprotected_write", "symlinked_skills"))
    if other_blocked:
        alerts.append("보류된 증류 작업 {0}건 (/distill-status)".format(other_blocked))
    if counts.get("failed"):
        alerts.append("실패한 증류 작업 {0}건 (/distill-status)".format(counts["failed"]))

    waiting = int(counts.get("pending") or 0) + int(counts.get("running") or 0)
    if waiting:
        try:
            if not queue.worker_alive():
                launched = distill_worker.launch_detached()
                if not launched.get("launched") and launched.get("reason") == "claude_not_found":
                    alerts.append(
                        "대기 중인 증류 작업 {0}건이 있으나 claude CLI 를 찾지 못했습니다 "
                        "(SIS_CLAUDE_BIN 으로 경로 지정 가능)".format(waiting))
        except Exception:
            pass

    _remember_seen(seen, [str(job["id"]) for job in fresh])
    return ("[claude-code-self-improving-skills] " + ", ".join(alerts) if alerts else None), reload_skills


def _remember_seen(seen, new_ids):
    """Persist which finished jobs have been reported.

    SessionStart fires again on resume and fork, so without this the same
    reload/alert would repeat on every one of them.
    """
    if not new_ids:
        return
    for job_id in new_ids:
        seen[job_id] = True
    if len(seen) > 200:
        seen = {k: True for k in sorted(seen, key=_as_int)[-200:]}
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(DISTILL_SEEN, "w", encoding="utf-8") as fh:
            json.dump(seen, fh)
    except Exception:
        pass


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _read_seen():
    try:
        with open(DISTILL_SEEN, encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _int_env(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _count_learned_skills():
    """Count learned skills this plugin distilled: DIRECT children of
    ~/.claude/skills with a SKILL.md carrying our provenance marker — the same
    counting rule as curator_transitions._learned_names. (A recursive walk
    would also count SKILL.md copies under support dirs like references/ or
    templates/, inflating the curation trigger — Hermes 9137b86a excludes
    support-dir SKILL.md from discovery for the same reason.)"""
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
            if PROVENANCE_KEY in head:
                learned += 1
        except Exception:
            pass
    return learned


def _read_curator_state():
    try:
        with open(CURATOR_STATE, encoding="utf-8") as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_curator_state(state):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = CURATOR_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, CURATOR_STATE)
    except Exception:
        pass


def _curation_status(learned_count):
    """'seed' (first ever — defer), 'due' (run now), or 'idle'."""
    state = _read_curator_state()
    if "last_run" not in state:
        return "seed"
    if learned_count < _int_env("SIS_CURATE_MIN_SKILLS", 8):
        return "idle"
    interval = _int_env("SIS_CURATE_INTERVAL_DAYS", 7) * 86400
    try:
        last = float(state.get("last_run", 0))
    except (TypeError, ValueError):
        last = 0.0
    return "due" if (time.time() - last) >= interval else "idle"


def _run_curator(state, lines):
    """Run the deterministic time-based transition pass inline and report it."""
    summary = None
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import curator_transitions
        summary = curator_transitions.run(dry_run=False)
    except Exception:
        summary = None
    state["last_run"] = time.time()
    state["run_count"] = int(state.get("run_count", 0)) + 1
    if summary:
        na, ns, nr = len(summary["archived"]), len(summary["stale"]), len(summary["reactivated"])
        state["last_summary"] = {"archived": na, "stale": ns, "reactivated": nr}
        if na or ns or nr:
            lines.append(
                "[큐레이터] 미사용 스킬 자동 정리 실행: stale {0}개, 아카이브 {1}개, 재활성화 {2}개. "
                "아카이브된 스킬은 ~/.claude/skills/.archive/ 로 이동(삭제 아님, /restore-skill 로 복구). "
                "세부 리포트는 ~/.claude/self-improve/logs/curator/.".format(ns, na, nr)
            )
        else:
            lines.append("[큐레이터] 정기 점검 완료 — 정리할 미사용 스킬이 없습니다.")
    _write_curator_state(state)


def _enqueue_curation(lines):
    """Hand the semantic consolidation pass to the background worker.

    The time-based transitions above are deterministic, so they run inline.
    Deciding that five narrow skills are really one — reading them and writing
    the umbrella — is judgement work that needs a model, and doing it inline
    would block the session start it was triggered from. So it goes on the same
    queue the Stop hook uses, with `trigger=curate`.

    Best-effort by construction: every failure path falls through to the manual
    pointer rather than raising, because a SessionStart hook must never be the
    reason a session doesn't start.
    """
    if (os.environ.get("SIS_AUTO_CURATE") or "").strip() == "0":
        lines.append("[큐레이터] 자동 통합은 꺼져 있습니다(SIS_AUTO_CURATE=0). "
                     "직접 정리하려면 /curate-skills 를 실행하세요.")
        return
    queued_id = None
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import distill_queue
        import distill_worker

        if distill_worker.discover_claude():
            queue = distill_queue.DistillQueue()
            result = queue.enqueue(
                session_id="curator",
                # One consolidation per UTC day at most: the queue keys
                # (session_id, prompt_id) uniquely, so parallel session starts
                # on the same day collapse to a single job instead of racing
                # several children over the same skill tree.
                prompt_id="curate-{0}".format(time.strftime("%Y%m%d", time.gmtime())),
                transcript_path="",
                transcript_rows=0,
                signal=False,
                signal_source="session_start",
                trigger=distill_worker.CURATE_TRIGGER,
            )
            queued_id = result.get("job_id")
            if queued_id:
                try:
                    distill_worker.launch_detached()
                except Exception:
                    # The job is durable; the next SessionStart relaunches it.
                    pass
    except Exception:
        queued_id = None

    if queued_id:
        lines.append(
            "[큐레이터] 중복 스킬 통합 패스를 백그라운드에 맡겼습니다 — 이 대화는 그대로 진행하시면 "
            "됩니다. 결과는 다음 세션 시작 시 보고되고, 되돌리려면 /curator-rollback 을 쓰세요."
        )
    else:
        lines.append("[큐레이터] 중복 스킬의 의미 기반 통합이 필요하면 /curate-skills 를 실행하세요.")


def main():
    # Inside Cowork the cowork variant announces its own loop; describing the
    # background queue there would promise persistence this session cannot give.
    if runtime_env.is_cowork_runtime():
        emit_context(None)

    try:
        sys.stdin.read()  # drain payload; we don't need its fields
    except Exception:
        pass

    try:
        learned = _count_learned_skills()
    except Exception:
        emit_context(None)

    mode = _review_mode()
    if mode == "off":
        # Nothing is watching this session, so saying the loop is active would
        # be false — and the background child runs in this mode too, where any
        # guidance we inject is pure noise it must ignore.
        emit_context(None)
    background = mode == "background"
    if background:
        lines = [
            "[claude-code-self-improving-skills] 자기개선 루프 활성 (백그라운드 모드). "
            "복잡한 작업이 끝나면 Stop 훅이 별도 프로세스에 증류를 맡깁니다 — "
            "이 대화에는 아무 것도 출력되지 않고 비용도 붙지 않으니, 증류를 위해 "
            "따로 할 일은 없습니다. 지금 당장 남기고 싶으면 /distill-skill 을 쓰세요.",
            "학습 스킬에서 낡거나 틀린 내용을 발견하면 그 자리에서 해당 SKILL.md 를 patch 하세요.",
        ]
    else:
        lines = [
            "[claude-code-self-improving-skills] 자기개선 루프 활성. 복잡한 작업·까다로운 디버깅·새 기법 발견을 "
            "끝냈고 재사용 가능하다면, /distill-skill 또는 Task(또는 Agent) 도구로 "
            "subagent_type=\"claude-code-self-improving-skills:skill-distiller\"(네임스페이스 접두사 생략 시 "
            "호출 실패)를 run_in_background=true 로 호출해 ~/.claude/skills 에 남기세요 — "
            "그냥 종료하면 Stop 훅이 한 번 상기시킵니다.",
            "학습 스킬에서 낡거나 틀린 내용을 발견하면 그 자리에서 해당 SKILL.md 를 patch 하세요. "
            "백그라운드 모드(기본)로 돌리면 이 위임 자체가 필요 없어집니다 — "
            "`claude setup-token` 후 ~/.claude/self-improve/worker.env 에 토큰을 넣으면 됩니다.",
        ]
    if learned:
        lines.append("현재 학습된 스킬 {0}개가 ~/.claude/skills 에 누적되어 있습니다.".format(learned))

    reload_skills = False
    if background:
        try:
            note, reload_skills = _background_note()
            if note:
                lines.append(note)
        except Exception:
            pass

    try:
        status = _curation_status(learned)
        if status == "seed":
            # First ever tick: seed the clock and DEFER (never curate on install).
            _write_curator_state({"last_run": time.time(), "run_count": 0})
        elif status == "due":
            _run_curator(_read_curator_state(), lines)
            if background:
                # Only in background mode: foreground/off means the user opted
                # out of this plugin spawning child sessions, and a consolidation
                # pass is exactly that.
                _enqueue_curation(lines)
    except Exception:
        pass

    emit_context("\n".join(lines), reload_skills=reload_skills)


# NOTE(v0.10.0): the always-on ~470-char permission-recovery hint that used to
# be injected here every session was replaced by one-clause pointers — in the
# intro lines above and in the Stop-hook nudge (where blocks actually happen).
# The full 5-rule recovery recipe lives in README "auto mode" 섹션. The design
# rationale stands: never predict a block from settings (skip flags, runtime
# modes, enterprise settings make that unreliable) — react to a REAL block.


if __name__ == "__main__":
    main()

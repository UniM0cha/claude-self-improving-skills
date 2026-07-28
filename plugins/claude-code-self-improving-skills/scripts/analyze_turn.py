#!/usr/bin/env python3
"""Stop-hook analyzer for the claude-code-self-improving-skills plugin.

Reads the Claude Code Stop-hook payload on stdin, measures how many tool calls
have accumulated since the last skill-distillation "anchor", and emits a
Stop-hook decision on stdout. If the work since the last distillation looks
substantial enough and nothing has been distilled, it BLOCKs and instructs the
agent to delegate to the skill-distiller subagent. Otherwise it APPROVEs.

Design notes — every one of these avoids a confirmed failure mode of the
sibling dev-log hook (which never fired across 396 real transcripts):

  * Tool calls are detected via the REAL transcript shape — an `assistant` row
    whose `message.content[]` contains `{"type":"tool_use","name":...}`.
    (dev-log grepped for `"tool":"Edit"`, which matches 0 transcripts; the real
    key is `"name":"Edit"`.)
  * "Already distilled?" is decided by an ACTUAL action — a Task delegation to
    skill-distiller, or a Write/Edit of a SKILL.md under ~/.claude/skills — not
    by a substring match on the word "distill"/the plugin name, which would
    self-trip because the plugin's own name/paths are injected into every
    transcript.
  * The block decision is emitted as JSON on STDOUT with exit 0 (the contract
    Claude Code actually parses), NOT on stderr with exit 2.
  * `stop_hook_active` is honored as a loop guard so we never re-block our own
    block.
  * A nudge is raised at most ONCE per segment of work: the row count at block
    time is persisted (usage_store `_meta.nudges`) and counting resumes from
    there, so a legitimately-declined nudge ("일회성이라 캡처 안 함") is not
    re-raised on every subsequent Stop — only after another threshold's worth
    of NEW work. The same start row bounds the core-touch (L1) advisory.
  * Any error fails safe to APPROVE — the hook must never wedge a session shut.

Config:
  SIS_DISTILL_THRESHOLD  tool calls since last distill required to nudge (default 12)
  SIS_MIN_FILE_EDITS     min real file edits (Edit/Write/MultiEdit/NotebookEdit)
                         since last distill, so pure read/search turns don't nudge (default 2)
"""

import json
import os
import re
import sys
import time
from typing import NoReturn

import runtime_env
import sis_io

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import usage_store
except Exception:
    usage_store = None  # telemetry is best-effort; nudge logic works without it

# Pin UTF-8 before any decision is written; a swallowed UnicodeEncodeError on
# the Korean nudge would otherwise degrade this hook to a silent approve.
sis_io.pin_utf8_stdio()

SKILL_MARKER = "skill-distiller"
EDIT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
SKILLS_DIR = os.path.expanduser("~/.claude/skills")


def emit(obj) -> NoReturn:
    """Write a Stop-hook decision to stdout and exit 0."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()
    sys.exit(0)


def approve() -> NoReturn:
    emit({"decision": "approve"})


def _int_env(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _tool_uses(row):
    """Yield tool_use blocks from an assistant row (real transcript shape)."""
    if not isinstance(row, dict) or row.get("type") != "assistant":
        return
    msg = row.get("message")
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield block


def _is_skill_path(file_path):
    """True if a path is a SKILL.md inside a ~/.claude/skills tree."""
    norm = str(file_path or "").replace("\\", "/")
    return "/.claude/skills/" in norm and norm.endswith("SKILL.md")


def _is_plugin_source_path(file_path):
    """True if a path is inside the claude-code-self-improving-skills plugin's OWN source
    tree (dev checkout, marketplace clone, or plugin cache) — NOT a distilled
    skill under ~/.claude/skills. Used to surface "you touched the plugin core"
    so the improvement can be routed upstream instead of (only) into skills.

    Matching on the "/claude-code-self-improving-skills/" path segment covers every install
    location. The state dir "~/.claude/self-improve/" has a different name and
    does not match. A distilled skill that happens to be named
    "claude-code-self-improving-skills" would live under "/.claude/skills/", so we exclude
    that first.
    """
    norm = str(file_path or "").replace("\\", "/")
    if "/.claude/skills/" in norm:
        return False
    if "/sis-pr-" in norm:
        # our own temp PR clone (propose_plugin_pr.py mkdtemp prefix) — editing
        # files there is the L2 flow itself, not a fresh edit of the user's tree
        return False
    return "/claude-code-self-improving-skills/" in norm


def _is_core_pr_action(command):
    """True if a Bash command handled a core change via a PR — submitting through
    the L2 helper, or creating a PR directly. Seeing this AFTER a core edit clears
    the core-touch advisory so the L1 notice doesn't keep re-firing on the same
    edit. We match the `submit` subcommand specifically (not `prepare`, which only
    clones) so a prepared-but-not-yet-submitted change still surfaces.
    """
    c = str(command or "")
    return "propose_plugin_pr.py submit" in c or "gh pr create" in c


def _learned_skill_names():
    """Names of learned skills = immediate dirs under ~/.claude/skills with a SKILL.md."""
    names = set()
    try:
        for entry in os.listdir(SKILLS_DIR):
            if entry.startswith("."):
                continue
            if os.path.isfile(os.path.join(SKILLS_DIR, entry, "SKILL.md")):
                names.add(entry)
    except Exception:
        pass
    return names


def _skill_name_from_path(file_path):
    """The skill name for a ~/.claude/skills/<name>/SKILL.md path (dir basename)."""
    if not _is_skill_path(file_path):
        return None
    norm = str(file_path).replace("\\", "/")
    return os.path.basename(os.path.dirname(norm)) or None


def _seed_created_by(name):
    """created_by for a usage record being seeded from a telemetry event.

    Only an explicit distilled/provenance marker in the skill's OWN frontmatter
    makes it "agent" (curation-eligible). Everything else defaults to "user" so
    merely OBSERVING a hand-authored skill being read can never put it on the
    curator's auto-archive track — provenance is decided by explicit markers,
    never inferred from location or usage (Hermes skill_usage rule)."""
    try:
        safe = os.path.basename(str(name))  # never let a name traverse out
        with open(os.path.join(SKILLS_DIR, safe, "SKILL.md"),
                  encoding="utf-8", errors="ignore") as fh:
            head = fh.read(2048)
        if re.search(r"origin\s*:\s*distilled", head) or \
                "provenance: self-improving-skills" in head:
            return "agent"
    except Exception:
        pass
    return "user"


def _capture_telemetry(rows, session_id):
    """Best-effort: bump use/view counters for learned skills from new
    transcript rows (since this session's last processed offset). Signals
    (verified against real transcripts):
      - Skill tool, input.skill (namespace-stripped) matches a learned skill -> use
      - Read of a ~/.claude/skills/**/SKILL.md                                -> view

    NOT counted here:
      - patch: counted by the PostToolUse validator instead — it also fires
        inside subagents, so the background distiller's edits (which live in a
        separate agent transcript this scanner never reads) keep the skill's
        idle clock fresh. Counting patches here too would double-count.
      - view during a maintenance segment: if this segment ran /curate-skills
        or /curator-status, their bulk SKILL.md reads are library maintenance,
        not usage — counting them would reset every stale candidate's idle
        clock on each curation pass and the time-based pruning would never fire.
    """
    if usage_store is None:
        return
    learned = _learned_skill_names()
    try:
        usage_store.forget_missing(learned)
    except Exception:
        pass

    offset = 0
    try:
        offset = usage_store.get_offset(session_id)
    except Exception:
        offset = 0
    if offset < 0:
        offset = 0
    elif offset > len(rows):
        # The transcript reads SHORTER than the mark we already recorded — it
        # is written asynchronously, so this is a stale read, not a rewind.
        # Rescanning from zero would re-count every event up to the mark as if
        # it were new, inflating usage counters and resetting the curator's
        # idle clocks. Skip this pass; the next one sees the full file.
        return

    events = []
    if learned:
        # created_by only matters when an event SEEDS a missing record; compute
        # it from the skill's own frontmatter (cached per name), default "user".
        cb_cache = {}

        def _cb(name):
            if name not in cb_cache:
                cb_cache[name] = _seed_created_by(name)
            return cb_cache[name]

        # Maintenance segment? (/curate-skills or /curator-status ran) —
        # suppress view events so curation can't reset idle clocks.
        maintenance = False
        for row in rows[offset:]:
            for tu in _tool_uses(row):
                if tu.get("name") == "Skill":
                    raw_inp = tu.get("input")
                    inp = raw_inp if isinstance(raw_inp, dict) else {}
                    sk = str(inp.get("skill", "")).split(":")[-1]
                    if sk in ("curate-skills", "curator-status"):
                        maintenance = True
                        break
            if maintenance:
                break

        for row in rows[offset:]:
            for tu in _tool_uses(row):
                name = tu.get("name")
                raw_inp = tu.get("input")
                inp = raw_inp if isinstance(raw_inp, dict) else {}
                if name == "Skill":
                    sk = str(inp.get("skill", "")).split(":")[-1]
                    if sk in learned:
                        events.append((sk, "use", _cb(sk)))
                elif name == "Read" and not maintenance:
                    sn = _skill_name_from_path(inp.get("file_path"))
                    if sn in learned:
                        events.append((sn, "view", _cb(sn)))
    try:
        usage_store.apply_events(events, session_id, len(rows))
    except Exception:
        pass


def resolve_review_mode():
    """background (default) | foreground | off."""
    mode = (os.environ.get("SIS_REVIEW_MODE") or "").strip().lower()
    return mode if mode in ("background", "foreground", "off") else "background"


PREFLIGHT_TTL_SECONDS = 3600


def _background_usable(distill_worker):
    """Whether a queued job could actually run — checked BEFORE queueing.

    Queueing on a machine where the CLI is missing, unauthenticated, or too old
    means the turn ends silently and the failure only surfaces later, in a
    blocked job nobody is looking at. The README promises a foreground nudge
    instead, so the decision has to be made here.

    The verdict is cached: `claude --version` plus `auth status` is two
    subprocesses, and this runs on a Stop hook.
    """
    claude_bin = distill_worker.discover_claude()
    if not claude_bin:
        return False

    cache_path = os.path.join(distill_worker.state_dir(), "preflight.json")
    now = time.time()
    try:
        with open(cache_path, encoding="utf-8") as fh:
            cached = json.load(fh)
        if (
            cached.get("bin") == claude_bin
            and now - float(cached.get("at") or 0) < PREFLIGHT_TTL_SECONDS
        ):
            return bool(cached.get("ok"))
    except Exception:
        pass

    try:
        env = distill_worker.child_environment()
        # hooks.json gives this hook 15 seconds in total. _run_cli defaults to
        # 30 per command, so a stalled CLI would have Claude kill the hook
        # before it could fall back to the foreground nudge.
        original = distill_worker._run_cli

        def _bounded(command, env, timeout=5):
            return original(command, env, timeout=min(timeout, 5))

        distill_worker._run_cli = _bounded
        try:
            blocked, _version = distill_worker._preflight(claude_bin, env)
        finally:
            distill_worker._run_cli = original
        ok = blocked is None
    except Exception:
        ok = False
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump({"bin": claude_bin, "at": now, "ok": ok}, fh)
    except Exception:
        pass
    return ok


def _enqueue_background(payload, session_id, transcript_path, rows,
                        nudge_fires, readonly_fires, core_fires):
    """Queue a detached distillation. False means "fall back to the nudge".

    Every failure path here is a fall-through rather than an error: a broken
    queue must degrade to the old in-session behaviour, never leave the user
    with no distillation at all.
    """
    try:
        import distill_queue
        import distill_worker
    except Exception:
        return False

    # NOTE(v0.17.0): a daily spawn cap used to sit here (12/day) as a runaway
    # guard. It was removed because it counted ATTEMPTS, not successes — a day
    # where every job failed still filled the quota and then silently refused
    # the next healthy one, which is the opposite of what a guard should do.
    # The real ceilings are per-job and still in force: `--max-budget-usd` on
    # every child, and the triggers themselves (threshold + edit floor), which
    # only fire once per segment of work.
    try:
        queue = distill_queue.DistillQueue()
    except Exception:
        return False

    if not _background_usable(distill_worker):
        return False

    if nudge_fires or readonly_fires:
        trigger = "signal" if nudge_fires else "readonly"
    else:
        trigger = "core_touch"
    try:
        queued = queue.enqueue(
            session_id=session_id,
            # Claude Code's Stop payload has no turn id; prompt_id (v2.1.196+)
            # is the stable per-prompt identifier that dedupes repeat Stops.
            prompt_id=str(payload.get("prompt_id") or ""),
            transcript_path=transcript_path,
            transcript_rows=rows,
            signal=bool(nudge_fires or readonly_fires),
            signal_source="stop_hook",
            trigger=trigger,
            # The transcript is written asynchronously and may not yet hold
            # this turn's final message; the payload always does.
            last_assistant_message=str(payload.get("last_assistant_message") or ""),
            cwd=str(payload.get("cwd") or ""),
        )
    except Exception:
        return False
    if not queued.get("job_id"):
        return False
    try:
        distill_worker.launch_detached()
    except Exception:
        # The job is durable; the next SessionStart relaunches the worker.
        pass
    return True


def main():
    # Cross-process recursion guard. The background worker runs a full Claude
    # Code session, so THIS hook fires inside it too — and it would enqueue
    # another job, which would spawn another worker, forever. `stop_hook_active`
    # cannot help: it marks a continuation within one session, and the worker's
    # child is a separate process where it arrives false. `CLAUDE_CODE_CHILD_SESSION`
    # cannot either: Claude Code sets it to 1 in every normal hook process, so
    # keying on it would disable the hook everywhere. Hence our own marker.
    # It is checked before ANY state is touched, so a background session also
    # cannot consume the session-map slots the foreground session needs.
    if os.environ.get("SIS_BACKGROUND_JOB") == "1":
        approve()

    # Exactly one self-improving-skills variant may act per session. This one
    # owns local Claude Code, where ~/.claude/skills survives the session and a
    # distilled skill can simply be written there. Inside Cowork the session
    # home is discarded, so the cowork variant runs instead — and queueing a
    # background `claude -p` against a home that is about to vanish would
    # produce a skill nobody ever sees.
    if runtime_env.is_cowork_runtime():
        approve()

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        approve()

    # Loop guard: never re-block inside the same Stop cycle.
    if payload.get("stop_hook_active"):
        approve()

    path = payload.get("transcript_path") or ""
    if not path or not os.path.isfile(path):
        approve()

    threshold = _int_env("SIS_DISTILL_THRESHOLD", 12)
    min_edits = _int_env("SIS_MIN_FILE_EDITS", 2)

    rows = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        approve()

    session_id = str(payload.get("session_id") or os.path.basename(path))

    # Telemetry capture (best-effort, isolated): record skill use/view/patch from
    # new transcript rows. Never let this affect the nudge decision below.
    try:
        _capture_telemetry(rows, session_id)
    except Exception:
        pass

    # Anchor = the last index at which a distillation ALREADY happened, i.e.
    #   (a) a subagent delegation to skill-distiller, or
    #   (b) a Write/Edit/MultiEdit whose file_path is a ~/.claude/skills SKILL.md.
    # Everything after the anchor is "work not yet distilled".
    #
    # NOTE: the subagent-spawning tool is named differently across Claude Code
    # surfaces ("Task" in the docs, "Agent" in some runtimes), and the
    # subagent_type may carry a plugin namespace prefix
    # ("claude-code-self-improving-skills:skill-distiller"). So we key on the *presence of a
    # subagent_type input* containing the distiller marker — environment- and
    # name-agnostic — rather than hardcoding the tool name. (Getting this wrong
    # is exactly the class of silent-mismatch bug dev-log hit; verified against a
    # real transcript where the tool name was "Agent", not "Task".)
    anchor = -1
    for i, row in enumerate(rows):
        for tu in _tool_uses(row):
            name = tu.get("name")
            raw_inp = tu.get("input")
            inp = raw_inp if isinstance(raw_inp, dict) else {}
            subagent_type = str(inp.get("subagent_type", ""))
            if SKILL_MARKER in subagent_type:
                anchor = i
            elif name in EDIT_TOOLS and _is_skill_path(inp.get("file_path")):
                anchor = i

    # Nudge-once guard: resume counting from the last nudge's row count, so a
    # nudge the agent already saw (and possibly declined for good reason) is
    # not re-raised every turn — only after another threshold of NEW work.
    nudged_rows = 0
    if usage_store is not None:
        try:
            nudged_rows = usage_store.get_nudge_row(session_id)
        except Exception:
            nudged_rows = 0
    if nudged_rows > len(rows):
        nudged_rows = 0  # stale marker (transcript rotated/shrank)
    start = max(anchor + 1, nudged_rows)

    # Count tool calls and real file edits since the anchor/last nudge.
    total_calls = 0
    file_edits = 0
    core_touched = False  # did this segment edit the plugin's OWN source?
    for row in rows[start:]:
        for tu in _tool_uses(row):
            total_calls += 1
            name = tu.get("name")
            raw_inp = tu.get("input")
            inp = raw_inp if isinstance(raw_inp, dict) else {}
            if name in EDIT_TOOLS:
                file_edits += 1
                if _is_plugin_source_path(inp.get("file_path")):
                    core_touched = True
            elif name == "Bash" and _is_core_pr_action(inp.get("command")):
                # core change was handled via a PR -> clear the advisory so it
                # doesn't keep re-firing on the same old edit in later Stops
                core_touched = False

    # Three independent triggers, merged into one block message:
    #   (1) nudge_fires — the undistilled segment is BOTH substantial (enough
    #       tool calls) AND has real artifacts (enough file edits). Keeps pure
    #       exploration/Q&A turns from triggering while staying broad across all
    #       kinds of work (unlike dev-log's compiled-build-only trigger).
    #   (2) readonly_fires — a substantial segment with ZERO file edits: long
    #       investigation/debugging/recon sessions are exactly where diagnostic
    #       techniques come from, and the edit floor would keep them from EVER
    #       being distilled. Hermes triggers on accumulated tool iterations
    #       alone; here the read-only path just carries a higher bar so short
    #       Q&A turns still never nudge.
    #   (3) core_touched — this segment edited the plugin's OWN source. Worth
    #       surfacing even for a tiny edit so the improvement can flow upstream
    #       (the "L1" advisory). PURELY INFORMATIONAL — it never auto-acts; the
    #       actual PR is opt-in and human-gated via /propose-plugin-improvement.
    nudge_fires = total_calls >= threshold and file_edits >= min_edits
    readonly_fires = (file_edits == 0
                      and total_calls >= _int_env("SIS_DISTILL_READONLY_THRESHOLD", 24))
    # core_touched has no threshold of its own: a one-line edit to the plugin
    # source trips it. That is fine for an advisory the agent reads, but as a
    # background trigger it would spawn a session on every single turn spent
    # working in this repository.
    core_fires = core_touched and total_calls >= _int_env("SIS_CORE_TOUCH_MIN_CALLS", 6)

    mode = resolve_review_mode()
    if mode == "off":
        approve()

    if mode == "background":
        if not (nudge_fires or readonly_fires or core_fires):
            # In particular a lone one-line core edit: it trips core_touched
            # with no threshold of its own, and blocking the turn for that
            # would break the silence background mode exists to provide.
            approve()
        queued = _enqueue_background(payload, session_id, path, len(rows),
                                     nudge_fires, readonly_fires, core_fires)
        if queued:
            if usage_store is not None:
                try:
                    usage_store.record_nudge(session_id, len(rows))
                except Exception:
                    pass
            approve()
        # Queue or CLI unavailable: fall through to the foreground nudge so
        # the loop degrades instead of silently doing nothing.

    if nudge_fires or readonly_fires or core_touched:
        parts = []
        if nudge_fires or readonly_fires:
            msg = (
                "이번 작업 구간에서 도구 호출이 {calls}회(파일 편집 {edits}회) 누적됐고 "
                "아직 스킬로 증류되지 않았습니다. 종료하기 전에 /distill-skill 을 실행하거나 "
                'claude-code-self-improving-skills:skill-distiller 서브에이전트'
                '(subagent_type="claude-code-self-improving-skills:skill-distiller" — 플러그인 네임스페이스 '
                '접두사를 빼면 호출이 실패함)를 Task(또는 Agent) 도구의 run_in_background=true 로 호출해 '
                '(반드시 백그라운드로 — 그래야 증류가 도는 동안 사용자 입력이 막히지 않습니다), '
                "이 세션에서 얻은 재사용 가능한 기법·패턴·해결책을 ~/.claude/skills 의 "
                "SKILL.md 로 캡처하세요.\n\n"
                "서브에이전트를 호출할 때 이 세션의 transcript 경로도 프롬프트에 포함하세요 "
                "(증류 근거를 직접 읽을 수 있게): {tpath}\n\n"
                "원칙:\n"
                "- 이미 관련된 기존 스킬이 있으면 새로 만들지 말고 그 SKILL.md 를 patch 하세요.\n"
                "- 한 번 쓰고 버릴 일회성 작업(특정 PR·특정 버그·환경 의존적 우회)이라면 "
                "캡처하지 말고 그대로 종료하세요.\n"
                "- 증류가 불필요하다고 판단되면, 그 이유를 사용자에게 한 줄로 알린 뒤 종료하세요."
            ).format(calls=total_calls, edits=file_edits, tpath=path)
            if readonly_fires and not nudge_fires:
                msg += ("\n\n이번 구간은 파일 편집이 없는 조사·디버깅 세션입니다 — 캡처 대상은 "
                        "코드 변경이 아니라 진단 기법·커맨드 사다리·원인 규명 패턴입니다. "
                        "그런 재사용 가능한 진단 지식이 없었다면 캡처하지 않는 것이 맞습니다.")
            # M1: opt-in cost routing for the distiller subagent. The
            # per-invocation model parameter beats the agent frontmatter, so no
            # file change is needed — default (unset) inherits the main model.
            model_pref = (os.environ.get("SIS_DISTILLER_MODEL") or "").strip().lower()
            if model_pref and model_pref != "haiku":  # 정책: 서브에이전트 Haiku 금지
                msg += ("\n\nSIS_DISTILLER_MODEL 이 설정되어 있습니다: 서브에이전트 호출에 "
                        "model=\"{0}\" 파라미터를 포함하세요(호출 시 지정한 model 이 에이전트 "
                        "frontmatter 보다 우선합니다).".format(model_pref))
            msg += ("\n\n백그라운드 호출이 \"권한 분류기에 의해 차단됨\"으로 막히면 — 그게 auto "
                    "mode 신호입니다. 플러그인 README 의 'auto mode' 섹션(permissions.allow "
                    "5줄)을 참조해 사용자에게 1회 추가를 안내하세요.")
            parts.append(msg)
        if core_touched:
            parts.append(
                "이번 구간이 claude-code-self-improving-skills 코어 소스를 직접 수정했습니다. "
                "이건 ~/.claude/skills 증류와는 별개입니다 — 플러그인 코어 개선은 사람이 "
                "유지보수하는 영역이므로, 다음 중 하나로 처리하세요:\n"
                "- 이 개선을 upstream(samton-inc/samton-plugins)에 기여하려면 "
                "/propose-plugin-improvement 를 실행하세요. fresh clone 에서 변경을 재현해 "
                "PR 로 제안합니다(write 권한이 없으면 fork 경유). "
                "opt-in: 환경변수 SIS_PLUGIN_PR=1 이 설정돼 있어야 실제 PR 을 만듭니다.\n"
                "- 또는 사람이 직접 브랜치/PR 로 처리하세요.\n"
                "자동 push·머지는 하지 않습니다. PR 제안까지만이며, 머지는 사람이 결정합니다."
            )
        # Remember this nudge so the same segment never re-triggers (the agent
        # may legitimately decline; that decision must stick).
        if usage_store is not None:
            try:
                usage_store.record_nudge(session_id, len(rows))
            except Exception:
                pass
        emit({"decision": "block", "reason": "\n\n———\n\n".join(parts)})

    approve()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise  # approve()/emit() exit normally — let them through
    except Exception:
        # Last-resort fail-safe: any unexpected error -> clean approve JSON,
        # never a traceback that could be read as a malformed Stop decision.
        approve()

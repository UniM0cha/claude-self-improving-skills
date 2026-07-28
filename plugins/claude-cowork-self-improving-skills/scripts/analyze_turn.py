#!/usr/bin/env python3
"""Stop-hook analyzer for the claude-cowork-self-improving-skills plugin.

Reads the Claude Code Stop-hook payload on stdin, measures how many tool calls
have accumulated since the last skill-distillation "anchor", and emits a
Stop-hook decision on stdout. If the work since the last distillation looks
substantial enough and nothing has been distilled, it BLOCKs and instructs the
agent to delegate to the skill-distiller subagent. Otherwise it APPROVEs.

Cowork adaptations (vs the original claude-code-self-improving-skills analyzer):

  * The nudge tells the agent to DELIVER the distilled/patched SKILL.md to the
    user via SendUserFile and guide them to the '스킬 저장' button — in Cowork
    the container's ~/.claude/skills vanishes at session end, so only skills
    saved to claude.ai survive into the next session.
  * Advisory fallback: if the once-per-session advisory (UserPromptSubmit hook)
    never ran when a nudge fires, a short version is prepended here and the
    shared flag is marked, so the loop rules are never entirely lost.
  * The core-touch (L1) advisory routes to the samton-plugins repo instead of
    /propose-plugin-improvement (not shipped in this variant), and the
    maintenance-segment view suppression keys on this variant's commands
    (/loop-status, /save-skill).

Inherited design notes (each avoids a confirmed failure mode of the sibling
dev-log hook, which never fired across 396 real transcripts):

  * Tool calls are detected via the REAL transcript shape — an `assistant` row
    whose `message.content[]` contains `{"type":"tool_use","name":...}`.
  * "Already distilled?" is decided by an ACTUAL action — a Task delegation to
    skill-distiller, or a Write/Edit of a SKILL.md under ~/.claude/skills — not
    by a substring match on the plugin name (which is injected into every
    transcript and would self-trip).
  * The block decision is emitted as JSON on STDOUT with exit 0 (the contract
    Claude Code actually parses), NOT on stderr with exit 2.
  * `stop_hook_active` is honored as a loop guard so we never re-block our own
    block.
  * A nudge is raised at most ONCE per segment of work: the row count at block
    time is persisted (usage_store `_meta.nudges`) and counting resumes from
    there, so a legitimately-declined nudge is not re-raised on every
    subsequent Stop — only after another threshold's worth of NEW work.
  * Any error fails safe to APPROVE — the hook must never wedge a session shut.

Config:
  SIS_DISTILL_THRESHOLD  tool calls since last distill required to nudge (default 12)
  SIS_MIN_FILE_EDITS     min real file edits (Edit/Write/MultiEdit/NotebookEdit)
                         since last distill, so pure read/search turns don't nudge (default 2)
  SIS_DISTILL_READONLY_THRESHOLD  tool calls for the zero-edit (investigation)
                         nudge path (default 24)
  SIS_DISTILLER_MODEL    opt-in model routing for the distiller subagent
"""

import json
import os
import re
import sys
from typing import NoReturn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runtime_env

try:
    import usage_store
except Exception:
    usage_store = None  # telemetry is best-effort; nudge logic works without it

SKILL_MARKER = "skill-distiller"
EDIT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
SKILLS_DIR = os.path.expanduser("~/.claude/skills")
ADVISORY_FLAG = os.path.expanduser("~/.claude/self-improve/advisory_shown")


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
    """True if a path is inside the claude-cowork-self-improving-skills plugin's OWN
    source tree (repo checkout or plugin cache) — NOT a distilled skill under
    ~/.claude/skills. Used to surface "you touched the plugin core" so the
    improvement can be routed to the samton-plugins repo instead of (only) into
    skills. The state dir "~/.claude/self-improve/" has a different name and
    does not match; a distilled skill living under "/.claude/skills/" is
    excluded first."""
    norm = str(file_path or "").replace("\\", "/")
    if "/.claude/skills/" in norm:
        return False
    return "/claude-cowork-self-improving-skills/" in norm


def _is_core_repo_action(command):
    """True if a Bash command routed a core change to the repo — a push or a PR.
    Seeing this AFTER a core edit clears the core-touch advisory so the L1
    notice doesn't keep re-firing on the same edit."""
    c = str(command or "")
    return "gh pr create" in c or "git push" in c


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
    makes it "agent". Everything else defaults to "user" so merely OBSERVING a
    hand-authored skill being read can never mislabel it — provenance is
    decided by explicit markers, never inferred from location or usage."""
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
        inside subagents, so the distiller's edits (which live in a separate
        agent transcript this scanner never reads) keep the skill's record
        fresh. Counting patches here too would double-count.
      - view during a maintenance segment: if this segment ran /loop-status or
        /save-skill, their bulk SKILL.md reads are library maintenance, not
        usage — counting them would inflate every skill's counters on each
        status/save pass."""
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
    if offset < 0 or offset > len(rows):
        offset = 0

    events = []
    if learned:
        # created_by only matters when an event SEEDS a missing record; compute
        # it from the skill's own frontmatter (cached per name), default "user".
        cb_cache = {}

        def _cb(name):
            if name not in cb_cache:
                cb_cache[name] = _seed_created_by(name)
            return cb_cache[name]

        # Maintenance segment? (/loop-status or /save-skill ran) — suppress
        # view events so status/save passes can't inflate usage counters.
        maintenance = False
        for row in rows[offset:]:
            for tu in _tool_uses(row):
                if tu.get("name") == "Skill":
                    raw_inp = tu.get("input")
                    inp = raw_inp if isinstance(raw_inp, dict) else {}
                    sk = str(inp.get("skill", "")).split(":")[-1]
                    if sk in ("loop-status", "save-skill"):
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


def _advisory_fallback():
    """If the once-per-session advisory (UserPromptSubmit) never ran, return a
    short version to prepend to the nudge and mark the shared flag — so the
    Cowork save rules are never entirely lost even if that hook is missing."""
    if os.path.isfile(ADVISORY_FLAG):
        return None
    try:
        os.makedirs(os.path.dirname(ADVISORY_FLAG), exist_ok=True)
        with open(ADVISORY_FLAG, "w", encoding="utf-8") as fh:
            fh.write("shown-by-stop-fallback\n")
    except Exception:
        pass
    return ("[claude-cowork-self-improving-skills] 자기개선 루프 활성 (세션 첫 안내가 유실되어 "
            "여기서 대신 전합니다): 이 컨테이너의 ~/.claude/skills 는 세션 종료 시 "
            "사라집니다. 증류·patch 한 SKILL.md 는 SendUserFile 로 사용자에게 보내 "
            "'스킬 저장' 버튼으로 claude.ai 에 등록하도록 안내해야 다음 세션에 남습니다 "
            "(미저장 스킬은 세션 도중 주기 동기화로 삭제될 수도 있음 — 즉시 저장). "
            "name 에 'claude'/'anthropic' 금지, description 에 꺾쇠 태그(<...>) 금지 "
            "— 둘 다 저장이 거부됩니다.")


def main():
    # Exactly one self-improving-skills variant may act per session. This one
    # owns Cowork, where the session home is thrown away and a learned skill
    # only survives by going through claude.ai. On a local machine the
    # claude-code variant handles the same events and writes straight to disk,
    # so staying silent here is what keeps the user from being told to press
    # "스킬 저장" for a skill that is already persisted.
    if runtime_env.is_local_runtime():
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

    # Telemetry capture (best-effort, isolated): record skill use/view from
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
    # surfaces ("Task" in the docs, "Agent" in Cowork), and the subagent_type
    # carries a plugin namespace prefix. So we key on the *presence of a
    # subagent_type input* containing the distiller marker — environment- and
    # name-agnostic — rather than hardcoding the tool name.
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
            elif name == "Bash" and _is_core_repo_action(inp.get("command")):
                # core change was routed to the repo -> clear the advisory so
                # it doesn't keep re-firing on the same old edit in later Stops
                core_touched = False

    # Three independent triggers, merged into one block message:
    #   (1) nudge_fires — the undistilled segment is BOTH substantial (enough
    #       tool calls) AND has real artifacts (enough file edits).
    #   (2) readonly_fires — a substantial segment with ZERO file edits: long
    #       investigation/debugging sessions are exactly where diagnostic
    #       techniques come from; the read-only path carries a higher bar so
    #       short Q&A turns still never nudge.
    #   (3) core_touched — this segment edited the plugin's OWN source. Worth
    #       surfacing even for a tiny edit so the improvement lands in the
    #       samton-plugins repo (a Cowork container copy vanishes at session
    #       end). PURELY INFORMATIONAL — it never auto-acts.
    nudge_fires = total_calls >= threshold and file_edits >= min_edits
    readonly_fires = (file_edits == 0
                      and total_calls >= _int_env("SIS_DISTILL_READONLY_THRESHOLD", 24))
    if nudge_fires or readonly_fires or core_touched:
        parts = []
        fb = _advisory_fallback()
        if fb:
            parts.append(fb)
        if nudge_fires or readonly_fires:
            msg = (
                "이번 작업 구간에서 도구 호출이 {calls}회(파일 편집 {edits}회) 누적됐고 "
                "아직 스킬로 증류되지 않았습니다. 종료하기 전에 /distill-skill 을 실행하거나 "
                "Task(또는 Agent) 도구로 skill-distiller 서브에이전트"
                '(subagent_type="claude-cowork-self-improving-skills:skill-distiller" — 플러그인 '
                "네임스페이스 접두사를 빼면 호출이 실패함)를 호출해, 이 세션에서 얻은 재사용 "
                "가능한 기법·패턴·해결책을 ~/.claude/skills 의 SKILL.md 로 캡처하세요.\n\n"
                "서브에이전트를 호출할 때 이 세션의 transcript 경로도 프롬프트에 포함하세요 "
                "(증류 근거를 직접 읽을 수 있게): {tpath}\n\n"
                "증류/patch 가 끝나면 — Cowork 필수 단계 — 해당 SKILL.md 를 SendUserFile 로 "
                "사용자에게 보내고 파일 카드의 '스킬 저장' 버튼으로 claude.ai 에 등록하도록 "
                "안내하세요. 이 컨테이너의 ~/.claude/skills 는 세션 종료 시 사라지므로, "
                "등록된 스킬만 다음 세션에 자동 동기화됩니다.\n\n"
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
            # Opt-in cost routing for the distiller subagent. The per-invocation
            # model parameter beats the agent frontmatter, so no file change is
            # needed — default (unset) inherits the main model.
            model_pref = (os.environ.get("SIS_DISTILLER_MODEL") or "").strip().lower()
            if model_pref and model_pref != "haiku":  # 정책: 서브에이전트 Haiku 금지
                msg += ("\n\nSIS_DISTILLER_MODEL 이 설정되어 있습니다: 서브에이전트 호출에 "
                        "model=\"{0}\" 파라미터를 포함하세요(호출 시 지정한 model 이 에이전트 "
                        "frontmatter 보다 우선합니다).".format(model_pref))
            parts.append(msg)
        if core_touched:
            parts.append(
                "이번 구간이 claude-cowork-self-improving-skills 코어 소스를 직접 수정했습니다. "
                "이건 ~/.claude/skills 증류와는 별개입니다 — Cowork 컨테이너 안의 플러그인 "
                "사본은 세션 종료 시 사라지고 다음 세션에는 claude.ai 에 등록된 버전이 다시 "
                "동기화되므로, 이 개선을 유지하려면 samton-plugins 리포"
                "(plugins/claude-cowork-self-improving-skills/)에 반영해야 합니다:\n"
                "- 리포 폴더가 연결되어 있으면 변경을 리포에 옮기고 CLAUDE.md 규칙"
                "(marketplace.json 과 plugin.json 의 version 동시 bump)대로 커밋하세요.\n"
                "- 아니면 변경 내용을 사용자에게 보고하고 리포 반영을 안내하세요.\n"
                "자동 push·머지는 하지 않습니다. 커밋/PR 은 사람이 결정합니다."
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

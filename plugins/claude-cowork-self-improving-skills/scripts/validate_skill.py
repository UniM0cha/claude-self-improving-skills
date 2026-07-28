#!/usr/bin/env python3
"""PostToolUse-hook logic for the claude-cowork-self-improving-skills plugin.

Runs after every Write/Edit/MultiEdit. It only acts when the edited file is a
SKILL.md under ~/.claude/skills (a learned skill); for everything else it stays
silent. For a learned skill it:

  1. Validates the on-disk SKILL.md against the Claude Code skill contract:
       - starts with a `---` frontmatter block that closes with `---`
       - frontmatter has a non-empty `name` (<=64 chars, lowercase/digits/hyphen)
       - frontmatter has a non-empty `description` (<=1024 chars)
       - non-empty body after the frontmatter
       - whole file <= 100000 chars
     and surfaces any problems back to the agent as additionalContext so it can
     fix them immediately.
  2. Stamps provenance: if the frontmatter has no `metadata:` provenance marker,
     it injects one (`metadata: { provenance: self-improving-skills, ... }`) so
     the advisory counter — and the original plugin's curator, when the skill
     is later synced to a local CLI machine — can tell agent-distilled skills
     apart from user-authored ones. The provenance VALUE stays identical to the
     original plugin's on purpose (cross-environment compatibility). Stamping
     never overwrites existing metadata and is skipped if it can't be done
     cleanly.
  3. Cowork addition: warns when a learned skill's `name` contains a claude.ai
     reserved word ('claude'/'anthropic') — such names are rejected by the
     '스킬 저장' button, which is this variant's only persistence path.

Output contract: print a PostToolUse JSON object with
`hookSpecificOutput.additionalContext` when there's something to say; otherwise
print nothing. Fails safe to silent on any error — validation feedback must
never break the edit that already happened.
"""

import json
import os
import re
import shutil
import sys
from typing import NoReturn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runtime_env

try:
    import usage_store
except Exception:
    usage_store = None

BACKUP_DIR = os.path.expanduser("~/.claude/self-improve/skill_backups")

MAX_NAME = 64
MAX_DESCRIPTION = 1024
DESC_WARN_LEN = 500  # soft advisory only — every session pays this in context
MAX_CONTENT = 100000
PROVENANCE_VALUE = "self-improving-skills"
# Hard charset rule (a violation BLOCKS + rolls back the edit).
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Official quick_validate also forbids trailing/consecutive hyphens — enforced
# as a non-blocking advisory only, so pre-existing learned skills with such
# names don't fall into an edit→rollback loop.
NAME_STRICT_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def silent() -> NoReturn:
    sys.exit(0)


def feedback(text) -> NoReturn:
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": text,
        }
    }, ensure_ascii=False))
    sys.exit(0)


def _is_learned_skill(file_path):
    norm = str(file_path or "").replace("\\", "/")
    return "/.claude/skills/" in norm and norm.endswith("SKILL.md")


def _backup_path(file_path):
    norm = str(file_path).replace("\\", "/")
    name = os.path.basename(os.path.dirname(norm))
    return os.path.join(BACKUP_DIR, name + ".bak")


def _rollback_if_possible(file_path):
    """Restore the pre-edit backup (made by backup_skill.py at PreToolUse).
    Returns True if a rollback happened (existing skill whose edit broke it),
    False if there was nothing to roll back to (a brand-new file)."""
    bp = _backup_path(file_path)
    if not os.path.isfile(bp):
        return False
    try:
        shutil.copy2(bp, file_path)
        return True
    except Exception:
        return False


def _split_frontmatter(text):
    """Return (frontmatter_str, body_str) or (None, None) if malformed."""
    if not text.startswith("---"):
        return None, None
    # find the closing '---' on its own line after the first
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not m:
        return None, None
    return m.group(1), m.group(2)


def _scalar(frontmatter, key):
    """Cheap YAML scalar read for top-level `key: value` (quoted or bare)."""
    for line in frontmatter.splitlines():
        m = re.match(r"^" + re.escape(key) + r"\s*:\s*(.*)$", line)
        if m:
            val = m.group(1).strip()
            if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
                val = val[1:-1]
            return val
    return None


def _validate(text):
    problems = []
    if len(text) > MAX_CONTENT:
        problems.append("파일이 너무 큽니다(>{0}자). references/ 로 본문을 분리하세요.".format(MAX_CONTENT))

    fm, body = _split_frontmatter(text)
    if fm is None:
        problems.append("YAML frontmatter 가 없습니다. 파일은 `---` 로 시작하고 `---` 로 닫혀야 합니다.")
        return problems

    name = _scalar(fm, "name")
    if not name:
        problems.append("frontmatter 에 `name` 이 없습니다.")
    else:
        if len(name) > MAX_NAME:
            problems.append("`name` 이 {0}자를 초과합니다.".format(MAX_NAME))
        if not NAME_RE.match(name):
            problems.append("`name` 은 소문자·숫자·하이픈만 사용해야 합니다(예: my-skill-name).")

    desc = _scalar(fm, "description")
    if not desc:
        problems.append("frontmatter 에 `description` 이 없습니다. 트리거 정확도의 핵심이니 "
                        "'이럴 때 사용한다'는 상황 중심으로 한 문장 작성하세요.")
    elif len(desc) > MAX_DESCRIPTION:
        problems.append("`description` 이 {0}자를 초과합니다.".format(MAX_DESCRIPTION))

    if not body or not body.strip():
        problems.append("frontmatter 뒤 본문(스킬 지침)이 비어 있습니다.")

    return problems


def _advisory(text, file_path=None):
    """Non-blocking quality advisories for a VALID skill (never trips rollback)."""
    fm, _body = _split_frontmatter(text)
    if fm is None:
        return None
    notes = []
    desc = _scalar(fm, "description") or ""
    if len(desc) > DESC_WARN_LEN:
        notes.append("description이 {0}자입니다. 학습 스킬의 description은 앞으로 모든 "
                     "세션의 시스템 프롬프트에 실리므로 길이가 곧 상시 컨텍스트 비용입니다. "
                     "트리거 문구는 보존하면서 {1}자 이하로 압축을 고려하세요."
                     .format(len(desc), DESC_WARN_LEN))
    name = _scalar(fm, "name") or ""
    if name and NAME_RE.match(name) and not NAME_STRICT_RE.match(name):
        notes.append("`name`에 선행·후행·연속 하이픈이 있습니다({0}). 공식 스킬 규약 위반이니 "
                     "디렉토리명과 함께 단어-사이-하이픈 형태로 바꾸는 것을 권장합니다."
                     .format(name))
    # Cowork: claude.ai '스킬 저장' rejects reserved words in the skill name
    # (실측 2026-07-16: "Skill name in SKILL.md cannot contain the reserved
    # word 'claude'"). That button is this variant's ONLY persistence path, so
    # surface it immediately while the writer can still rename cheaply.
    if name and ("claude" in name or "anthropic" in name):
        notes.append("`name`('{0}')에 claude.ai 예약어('claude'/'anthropic')가 들어 있습니다. "
                     "이 이름은 '스킬 저장' 버튼에서 거부되어 다음 세션으로 영속화할 수 없으니, "
                     "지금 디렉토리명과 name 을 함께 예약어 없는 이름으로 바꾸세요."
                     .format(name))
    # Cowork: claude.ai '스킬 저장' rejects XML-tag-like text in the description
    # (실측 2026-07-16: "SKILL.md description cannot contain XML tags" — a
    # `mnt/<folder>` placeholder tripped it). Body angle brackets are fine
    # (anthropic registry skills carry them); only the description matters.
    if re.search(r"<[^\s<>][^<>]*>", desc):
        notes.append("description 에 꺾쇠 태그 형태(<...>)가 있습니다. claude.ai '스킬 저장'이 "
                     "XML 태그로 간주해 거부하니, 지금 placeholder 를 일반 표기(예: "
                     "mnt/<folder> → mnt/폴더명)로 바꾸세요. 본문(body)의 꺾쇠는 무방합니다.")
    # name ≠ dir mismatch: usage telemetry keys on the DIR name — a mismatch
    # silently splits the record. Advisory only (never blocking, so a
    # pre-existing mismatched skill can't fall into an edit→rollback loop).
    dirname = os.path.basename(os.path.dirname(str(file_path or "").replace("\\", "/")))
    if name and dirname and name != dirname:
        notes.append("frontmatter name('{0}')과 디렉토리명('{1}')이 다릅니다. usage 텔레메트리는 "
                     "디렉토리명으로 집계되므로 어긋납니다 — 디렉토리명 또는 name 을 "
                     "일치시키는 것을 권장합니다."
                     .format(name, dirname))
    if notes:
        return "[claude-cowork-self-improving-skills] 참고:\n- " + "\n- ".join(notes)
    return None


def _frontmatter_has_pin(text):
    """`pinned: true` inside the CLOSED frontmatter block only — a
    `pinned: true` example in a skill's body must not count. Trailing
    inline comments (`pinned: true # keep`) are valid YAML and count."""
    fm, _ = _split_frontmatter(text or "")
    if fm is None:
        return False
    return bool(re.search(r"^\s*pinned\s*:\s*true\b", fm, re.I | re.M))


def _pinned_guard(file_path, payload, current_text):
    """C5 (Hermes 525e1e77): an AUTONOMOUS distiller edit to a pinned skill is
    rolled back — unattended maintenance has no user present to consent.
    Foreground (human-driven) edits stay allowed: same asymmetry as Hermes.

    Pinned is decided from the usage record, the PRE-edit backup's frontmatter
    (the edit itself could have stripped the marker), or — for a brand-new
    file with no backup/record — the written frontmatter itself (a distiller
    must not CREATE a curator-protected pinned skill unnoticed)."""
    agent_type = str(payload.get("agent_type") or "")
    if "skill-distiller" not in agent_type:
        return None
    norm = str(file_path).replace("\\", "/")
    name = os.path.basename(os.path.dirname(norm))
    pinned = False
    if usage_store is not None:
        try:
            pinned = bool(usage_store.all_records().get(name, {}).get("pinned"))
        except Exception:
            pinned = False
    if not pinned:
        bp = _backup_path(file_path)
        try:
            if os.path.isfile(bp):
                with open(bp, encoding="utf-8", errors="ignore") as fh:
                    pinned = _frontmatter_has_pin(fh.read())
            else:
                pinned = _frontmatter_has_pin(current_text)  # new file
        except Exception:
            pass
    if not pinned:
        return None
    if _rollback_if_possible(file_path):
        return ("[claude-cowork-self-improving-skills] '{0}' 은 pinned 스킬입니다 — 자율 증류(skill-distiller)는 "
                "pinned 스킬을 수정할 수 없어 편집 직전 버전으로 롤백했습니다. 이 변경이 정말 "
                "필요하면 내용을 사용자에게 보고하고 unpin 여부를 물어보세요.".format(name))
    # Nothing to roll back to (the distiller CREATED a new pinned skill) —
    # deleting a brand-new file is riskier than leaving it; warn only.
    return ("[claude-cowork-self-improving-skills] '{0}' 은 pinned 스킬입니다 — 자율 증류가 수정할 대상이 "
            "아닙니다. 변경 내용을 사용자에게 보고하고 승인/unpin 을 요청하세요.".format(name))


def _stamp_provenance(path, text):
    """Inject a provenance metadata marker if none exists. Best-effort."""
    if PROVENANCE_VALUE in text:
        return  # already stamped (or mentioned) — don't touch
    fm, body = _split_frontmatter(text)
    if fm is None or body is None:
        return
    if re.search(r"^metadata\s*:", fm, re.MULTILINE):
        return  # author already manages metadata; leave it alone
    new_fm = fm.rstrip("\n") + (
        "\nmetadata:\n"
        "  provenance: {0}\n".format(PROVENANCE_VALUE)
    )
    new_text = "---\n" + new_fm + "\n---\n" + body
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_text)
    except Exception:
        pass


def _record_patch(file_path, text, payload):
    """Record a patch event for this learned-skill write (seeding the usage
    record on first sight).

    Patch counting lives HERE (PostToolUse) and not in the Stop-hook transcript
    scan, because this hook also fires inside subagents: the background
    skill-distiller's edits land in a separate agent transcript the Stop
    scanner never reads — counting there would let an actively-maintained
    skill look idle and get auto-archived.

    created_by precedence for seeding: the writing agent's type (hook payload
    `agent_type` — present when the hook fires inside a subagent), then an
    explicit `origin: distilled` marker in the written text, else "user"."""
    if usage_store is None:
        return
    norm = str(file_path).replace("\\", "/")
    name = os.path.basename(os.path.dirname(norm))
    if not name:
        return
    agent_type = str(payload.get("agent_type") or "")
    if "skill-distiller" in agent_type or re.search(r"origin\s*:\s*distilled", text):
        created_by = "agent"
    else:
        created_by = "user"
    try:
        usage_store.apply_events([(name, "patch", created_by)])
    except Exception:
        pass


def main():
    # This validator also warns about names claude.ai's "스킬 저장" rejects,
    # which is advice a local session has no use for — there the claude-code
    # validator runs instead.
    if runtime_env.is_local_runtime():
        silent()

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        silent()

    raw_input = payload.get("tool_input")
    tool_input = raw_input if isinstance(raw_input, dict) else {}
    file_path = tool_input.get("file_path", "")
    if not _is_learned_skill(file_path) or not os.path.isfile(file_path):
        silent()

    try:
        with open(file_path, encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except Exception:
        silent()

    # Autonomous-distiller writes to a pinned skill roll back before anything
    # else — the guard supersedes validation (the edit is not allowed at all).
    try:
        guard_msg = _pinned_guard(file_path, payload, text)
    except Exception:
        guard_msg = None
    if guard_msg:
        feedback(guard_msg)

    problems = _validate(text)
    if not problems:
        _stamp_provenance(file_path, text)
        _record_patch(file_path, text, payload)
        adv = _advisory(text, file_path)
        if adv:
            feedback(adv)
        silent()

    # 구조가 깨짐 → 편집 직전 백업이 있으면 롤백(트랜잭션 안전), 없으면(신규) 경고만.
    if _rollback_if_possible(file_path):
        msg = (
            "[claude-cowork-self-improving-skills] {0} 편집이 SKILL.md 구조를 깨뜨려 편집 직전 버전으로 "
            "자동 롤백했습니다. 발견된 문제:\n- ".format(file_path)
            + "\n- ".join(problems)
            + "\n원본이 복원됐으니, 위 문제를 피해 다시 편집하세요."
        )
    else:
        msg = (
            "[claude-cowork-self-improving-skills] 방금 작성한 학습 스킬 {0} 에 문제가 있습니다:\n- ".format(file_path)
            + "\n- ".join(problems)
            + "\n수정한 뒤 다시 저장하세요."
        )
    feedback(msg)


if __name__ == "__main__":
    main()

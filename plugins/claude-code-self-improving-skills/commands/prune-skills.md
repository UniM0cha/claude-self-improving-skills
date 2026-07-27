---
description: N일 이상 미사용된 학습 스킬을 일괄 아카이브한다 (먼저 dry-run 미리보기)
---

오래 안 쓰인 학습 스킬을 일괄 정리하세요. 이것은 시간 기반 자동 큐레이터를 사람이 직접 당겨 실행하는 버전입니다 (이제 usage telemetry가 있으므로 가능).

`$ARGUMENTS` 에 일수가 주어졌으면 그 값을, 없으면 `SIS_ARCHIVE_AFTER_DAYS`(기본 90) 를 임계로 씁니다.

### 1. 미리보기 (먼저 반드시 실행 — 아무것도 바꾸지 않음)

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/curator_transitions.py" prune <days>
```

후보 목록(이름 + 미사용 일수 + use_count)을 사용자에게 표로 보여주세요. 대상은 **pin 안 됨 + `created_by:agent` + 아직 active/stale** 인 스킬만입니다 (사용자 작성·pin 스킬은 제외). use_count가 높은 후보는 "드물지만 결정적으로 쓰이는" 스킬일 수 있으니 사용자에게 짚어주세요.

### 2. 적용 (사용자 확인 후에만)

후보가 맞다면, 사용자에게 확인을 받은 뒤 실제 아카이브:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/curator_transitions.py" prune <days> --apply
```

`--apply` 는 변경 전 tar.gz 스냅샷을 뜨고, 후보들을 `~/.claude/skills/.archive/` 로 이동(삭제 아님)합니다. 개별 복구는 `/restore-skill`, 이번 정리 전체를 되돌리려면 `/curator-rollback` (스냅샷 + usage 메타 복원) 을 쓰세요.

> 단일 스킬만 정리하려면 `/archive-skill <name>` 을, 특정 스킬을 보호하려면 `/pin-skill <name>` 을 쓰세요.

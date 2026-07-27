---
description: 자기개선 루프 상태를 조회한다 — 학습 스킬별 사용 빈도·상태·마지막 큐레이션
---

자기개선 루프의 현재 상태를 사람이 읽기 좋게 보여주세요.

다음 데이터를 읽어 정리합니다:

```bash
echo "=== 큐레이터 상태 ==="
cat ~/.claude/self-improve/curator_state.json 2>/dev/null || echo "(아직 큐레이션 실행 안 됨)"
echo; echo "=== 스킬별 usage telemetry ==="
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/usage_store.py" dump 2>/dev/null || echo "(telemetry 없음)"
echo; echo "=== 아카이브된 스킬 ==="
ls -1 ~/.claude/skills/.archive/ 2>/dev/null || echo "(없음)"
```

그 다음, 위 JSON을 파싱해 **표로** 정리해 보여주세요:

- 각 학습 스킬: 이름 · state(active/stale/archived) · use/view/patch 횟수 · 마지막 사용 후 경과일 · pinned 여부 · created_by(agent/user)
- 마지막 큐레이션 실행 시각과 run_count, 직전 요약(stale/archived/reactivated 개수)
- 가장 오래 미사용된 스킬 상위 몇 개를 짚어, 곧 stale/archive 될 후보로 안내

`$ARGUMENTS` 가 특정 스킬 이름이면 그 스킬의 상세만 보여주세요.

> 임계값: 마지막 활동 후 SIS_STALE_AFTER_DAYS(기본 30일) → stale, SIS_ARCHIVE_AFTER_DAYS(기본 90일) → archive. 단 **누적 use_count ≥ 3인 스킬은 archive 임계가 2배**(검증된 스킬은 천천히 늙음). pin된 스킬과 사용자 작성(created_by=user) 스킬은 자동 정리 대상이 아닙니다.

---
description: 큐레이터 변경 전 스냅샷으로 학습 스킬 라이브러리 전체를 되돌린다 (usage 메타 포함, 롤백 자체도 언두 가능)
---

큐레이터(자동 전이·/prune-skills·/curate-skills)가 만든 변경을 스냅샷 시점으로 통째로 되돌리세요. 개별 스킬 하나만 복구하려면 이 커맨드가 아니라 `/restore-skill` 을 쓰세요.

먼저 스냅샷 목록을 보여주세요 (오래된 것부터):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/curator_backup.py" list
```

`$ARGUMENTS` 로 스탬프(예: `20260713T090000Z`)가 주어졌으면 그것을, 아니면 사용자에게 어느 시점으로 되돌릴지 확인하세요. **되돌리기 전에 반드시 사용자 확인을 받으세요** — 스냅샷 이후에 새로 생긴 스킬·수정은 라이브러리에서 사라집니다(단, 아래 언두 스냅샷으로 복구 가능).

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/curator_backup.py" rollback [<stamp>]
```

이 명령의 동작 (Hermes curator_backup 이식):

- 롤백 **직전의 현재 트리를 먼저 새 스냅샷으로 저장**합니다 — 롤백 자체가 언두 가능하며, 결과 JSON의 `undo_snapshot` 이 그 이름입니다. 되돌린 걸 다시 되돌리려면 그 스탬프로 rollback을 한 번 더 실행하세요.
- 스킬 트리와 함께 **usage 텔레메트리(`skill_usage.json`)도 스냅샷 시점으로 복원**합니다 — 트리만 수동으로 풀면 usage 레코드가 `state: archived` 로 남아 복원된 스킬이 라이프사이클에서 빠집니다.
- `curator_state.json` 은 복원하지 않고 last_run만 현재 시각으로 갱신합니다 (옛 last_run을 복원하면 큐레이터가 더 빨리 재발화하는 역효과).
- 결과에 `usage_meta_restored: false` 가 있으면 v0.10.0 이전 구버전 스냅샷(메타 미수록)입니다 — 트리는 복원됐지만 usage 상태가 어긋날 수 있으니 `/curator-status` 로 점검을 안내하세요.

결과 JSON(`ok`, `restored_from`, `undo_snapshot`)을 사용자에게 요약해 주세요. 실패(`ok: false`) 시 `reason` 을 보여주고 라이브러리는 변경되지 않았음을 알리세요.

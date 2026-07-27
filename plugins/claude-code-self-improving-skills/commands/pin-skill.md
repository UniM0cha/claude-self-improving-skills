---
description: 학습 스킬을 pin하여 큐레이터의 자동 stale/archive 대상에서 제외한다
---

`$ARGUMENTS` 로 지정된 학습 스킬을 pin 하세요. pin된 스킬은 미사용 기간과 무관하게 큐레이터가 stale 처리하거나 아카이브하지 않습니다 (가치 있는 umbrella 스킬을 보존할 때 사용).

스킬 이름이 `$ARGUMENTS` 로 주어졌으면 그 이름으로, 아니면 `~/.claude/skills/` 의 학습 스킬 목록을 보여주고 어떤 것을 pin할지 물어보세요.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/usage_store.py" pin "<skill-name>"
```

unpin 하려면 `pin` 대신 `unpin` 을 쓰세요. 완료 후 한 줄로 결과를 안내하세요.

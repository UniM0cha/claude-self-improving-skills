---
description: claude-code-self-improving-skills 코어 개선을 upstream 리포에 PR로 제안한다 (opt-in, fresh clone, 자동 머지 없음)
---

claude-code-self-improving-skills **플러그인 자체**의 개선을 upstream(`samton-inc/samton-plugins`)에 PR로 제안하세요.

이것은 자기개선 루프의 **코어 기여 단계(L2)**입니다. distiller가 `~/.claude/skills`에 증류하는 것과 달리, 이건 플러그인 *소스*를 고쳐 upstream에 올립니다 — 격리된 fresh clone에서만 작업하고, **PR까지만(자동 push·머지 없음, 머지는 maintainer)**, write 권한이 없으면 자동으로 fork를 경유합니다.

> 언제: Stop 훅이 "코어 소스를 건드렸다"고 알렸거나, 이 플러그인의 버그/개선을 발견해 upstream에 기여하려 할 때. `$ARGUMENTS`로 무엇을 개선하는지 설명을 받습니다.

## 절차

### 1. opt-in 확인 (먼저 반드시)

```bash
printenv SIS_PLUGIN_PR
```

값이 비어 있으면 **여기서 중단**하고 사용자에게 안내하세요:

> L2 자동 PR 제안은 opt-in입니다. `~/.claude/settings.json`의 `env`에 `"SIS_PLUGIN_PR": "1"`을 추가한 뒤 다시 실행하세요. (원치 않으면 코어 개선을 사람이 직접 PR로 올려도 됩니다.)

### 2. 격리 clone 준비

개선을 한두 단어로 요약한 slug로:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/propose_plugin_pr.py" prepare <slug>
```

출력 JSON의 `dir`(작업 디렉토리)와 `mode`(`direct`=write 권한 / `fork`=fork 경유)를 확인하세요. gh 인증이 없으면 여기서 명확한 에러로 멈춥니다.

### 3. clone에서 개선 적용

`dir` 안의 `plugins/claude-code-self-improving-skills/` 소스에 이번 개선을 적용하세요.

- 방금 세션에서 코어를 이미 수정했다면, **그 변경을 이 clone에 동일하게 재현**하세요(원래 작업 트리가 아니라 clone에서).
- **transcript 내용·로컬 경로·비밀은 절대 넣지 마세요.** PR에는 소스 변경만 들어갑니다.
- 디버그 노트·임시 파일이 필요하면 **clone 밖**(`/tmp` 등)에 두세요. clone 안 `plugins/claude-code-self-improving-skills/` 아래에 두면 화이트리스트 서브트리에 걸려 PR에 섞일 수 있습니다(`submit`은 화이트리스트 *밖* staged는 거부하지만, 서브트리 *안* 임시파일까지는 막지 못합니다).

### 4. 검증 + 버전 동시 bump

수정한 파일을 검증하고, 버전 두 곳을 같은 값으로 올리세요:

```bash
python3 -m py_compile <수정한 .py 파일들>
python3 -m json.tool <수정한 .json> >/dev/null
```

- `dir/plugins/claude-code-self-improving-skills/.claude-plugin/plugin.json`의 `version`
- `dir/.claude-plugin/marketplace.json`의 claude-code-self-improving-skills entry `version`

(둘을 함께 올려야 marketplace와 플러그인 메타데이터가 어긋나지 않습니다.)

### 5. PR 생성

PR 본문을 **clone 밖** 임시 파일(예: `mktemp`)에 작성하세요 — **변경 요약과 근거만**, transcript 금지. 그 다음:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/propose_plugin_pr.py" submit "<dir>" "<PR 제목>" "<body 파일 경로>"
```

`submit`은 화이트리스트 경로(`plugins/claude-code-self-improving-skills/`, `.claude-plugin/marketplace.json`)만 스테이징해 커밋·push하고 upstream으로 PR을 연 뒤, 출력으로 **PR URL**을 줍니다. 그 URL을 사용자에게 보고하세요.

### 6. 마무리

- `mode`가 `fork`였다면, PR이 upstream으로 갔고 **머지는 maintainer가 결정**함을 알리세요.
- `submit`은 성공 시 temp clone을 자동 정리합니다. 실패했다면 `dir`이 남아 있으니 에러 메시지를 사용자에게 그대로 전하세요.

> 참고: 이 커맨드는 플러그인 *소스* 기여 전용입니다. 일반 작업에서 얻은 재사용 기법은 `/distill-skill`(→ `~/.claude/skills`)로 증류하세요 — 둘은 책임이 다릅니다.

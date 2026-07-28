# ChatGPT Work Self Improvement

ChatGPT Work에서 완료된 작업을 검토해 재사용 가능한 교훈만 제안하고, 사용자가 별도 메시지로 승인한 내용만 프로젝트 지침·overlay·`SKILL.md`로 내보내는 플러그인입니다.

## 왜 Codex판과 분리했나

기존 `chatgpt-codex-self-improving-skills`는 로컬 훅, MCP 서버, 로컬 transcript와 상태 저장소를 사용합니다. ChatGPT 웹은 그 로컬 런타임에 접근하지 않으므로 이 플러그인은 다음 원칙의 skills-only 패키지로 따로 구성합니다.

- 현재 대화와 대화에 제공된 파일만 사용
- 훅, MCP, 로컬 스크립트, 백그라운드 작업 없음
- 이전 대화·로컬 스킬·telemetry를 읽었다고 주장하지 않음
- 제안과 승인을 반드시 서로 다른 턴으로 분리
- 승인된 내용만 복사 가능한 지침, overlay, 패치, 새 스킬 후보로 내보냄
- 설치·게시·영속화가 실제로 이루어졌다고 대신 주장하지 않음

따라서 데스크톱 호스트가 꺼져 있어도, workspace에 공유되어 웹에 설치된 뒤에는 동일한 검토 스킬이 클라우드 대화 안에서 동작할 수 있습니다. 다만 Codex판처럼 매 턴 종료 시 자동 실행되지는 않습니다.

## 구성

| 구성 요소 | 역할 |
|---|---|
| `.codex-plugin/plugin.json` | Work용 skills-only 플러그인 manifest |
| `skills/work-self-improvement-review/SKILL.md` | 검토, 승인, 안전한 내보내기 계약 |
| `skills/work-self-improvement-review/agents/openai.yaml` | Work 표시명과 `@` 호출 프롬프트 |
| `evals/review_cases.json` | 긍정 5개·부정 3개 행동 검증 사례 |
| `tests/` | 배포 구조와 Work 호환성 계약 테스트 |

## 사용

1. 작업과 교정이 포함된 같은 대화에서 `@work-self-improvement-review`를 명시해 검토를 요청합니다.
2. 플러그인은 `status: pending` 후보 또는 `status: no-change` 결과만 반환합니다.
3. 후보를 채택하려면 다음 메시지에서 `WSI-001`처럼 후보 ID와 대상을 명시해 승인합니다.
4. 승인 후에만 프로젝트 지침, overlay, `SKILL.md` 패치 또는 새 `SKILL.md` 후보를 내보냅니다.

새 대화에서는 이전 대화를 자동으로 볼 수 없습니다. 이전 작업을 검토하려면 필요한 기록이나 파일을 그 대화에 제공해야 합니다.

## 데스크톱에서 웹으로 배포

1. 저장소의 루트 marketplace를 처음 등록하거나 기존 등록을 갱신합니다. 상황에 맞는 명령 하나를 실행하세요.

   ```bash
   # 최초 등록
   codex plugin marketplace add UniM0cha/self-improving-skills

   # 이미 등록된 marketplace 갱신
   codex plugin marketplace upgrade self-improving-skills
   ```

2. ChatGPT 데스크톱 앱을 재시작하고 Work 모드의 Plugins에서 `Self-Improving Skills` → `chatgpt-work-self-improving-skills`를 설치합니다. 현행 데스크톱 앱에서는 Codex 변형도 같은 marketplace에 함께 보이므로 이름을 확인해 Work 변형을 선택하세요.
3. 새 Work 대화에서 `@` 목록에 플러그인 또는 bundled skill이 나타나는지 확인합니다.
4. 데스크톱의 `Created by you`에서 workspace에 공유합니다.
5. ChatGPT 웹의 `Shared with you`에서 설치하고 새 대화를 시작합니다.
6. 데스크톱 앱을 완전히 종료한 상태에서 웹의 명시 호출, 자연어 호출, 민감정보 거절 사례를 확인합니다.

공유와 웹 설치는 workspace 또는 계정 상태를 변경하므로 실제 검증 시 사용자의 확인을 받은 뒤 진행합니다.

## 검증

```bash
UV_CACHE_DIR=/private/tmp/samton-work-plugin-uv \
uv run --with pytest --with pyyaml python -m pytest \
plugins/chatgpt-work-self-improving-skills/tests -q
```

공식 플러그인·스킬 validator도 함께 실행해야 합니다. 로컬 설치 성공만으로 웹 호환을 판정하지 않고, 마지막 배포 절차의 웹 새 대화까지 통과해야 완료로 봅니다.

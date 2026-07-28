# claude-code-self-improving-skills

복잡한 작업을 끝낼 때마다, 거기서 얻은 **재사용 가능한 기법을 자동으로 `SKILL.md`로 증류**하고 기존 스킬을 스스로 개선하는 자기개선 루프. Nous Research의 [Hermes Agent](https://github.com/NousResearch/hermes-agent)가 가진 "closed learning loop"를 Claude Code 플러그인 프리미티브(훅·서브에이전트·커맨드)로 이식한 것입니다.

> 한 세션에서 배운 것이 다음 세션의 자신에게 남습니다. 경험 → 증류 → 재사용.

## 동작 방식 (4층 루프)

| 층 | 구현 | 무엇을 하나 |
|---|---|---|
| **STORE** | `~/.claude/skills/<name>/SKILL.md` | 학습된 스킬을 전역 사용자 디렉토리에 누적. 플러그인 업데이트와 무관하게 지식이 쌓임 |
| **TRIGGER** | `Stop` 훅 (`distill-nudge.sh` → `analyze_turn.py`) | 마지막 증류 이후 도구 호출이 임계 이상 누적됐는데 아직 스킬화되지 않았으면, 종료를 한 번 막고 증류를 유도 |
| **REVIEW** | `skill-distiller` 서브에이전트 | 격리된 컨텍스트에서 `patch > umbrella수정 > 참조추가 > 신규생성` 우선순위로 판단해 SKILL.md를 쓰거나 고침 |
| **DISCOVER** | Claude Code 기본 동작 | 세션 시작 시 `skills/`를 자동 재스캔 → 새 스킬이 다음 세션 프롬프트에 자동 등장 |

부가:

- **TELEMETRY** (v0.2.0) — `Stop` 훅이 transcript에서 학습 스킬의 **사용 빈도를 추적**: `Skill` 호출→use, SKILL.md `Read`→view, `Write/Edit`→patch. `~/.claude/self-improve/skill_usage.json`에 use/view/patch 카운트 + 마지막 사용 시각 + `created_at` + `created_by`(agent/user)를 기록(atomic+flock, 세션별 offset으로 중복 방지). 이게 큐레이터가 "실제 안 쓰는 스킬"을 식별하는 데이터 기반입니다.
- **VALIDATE + 트랜잭션 편집** (v0.5.0) — `PreToolUse` 훅이 학습 SKILL.md를 편집 **직전에 백업**하고, `PostToolUse` 훅이 편집 후 frontmatter·크기를 검증. 편집이 구조를 깨뜨리면 **백업에서 자동 롤백**(Hermes `_patch_skill`의 backup→re-validate→rollback 이식)하고 모델에 다시 시도하도록 알림. 정상 편집은 무간섭. 처음 만들어진 학습 스킬엔 `metadata.provenance` 자동 부착 + usage 레코드 시딩(티어링: distiller=agent, 사용자 직접=user).
- **CURATE** (v0.3.0) — **시간기반 미사용 스킬 자동 정리**. `SessionStart` 훅이 큐레이션 주기(기본 7일)가 됐는지 확인하고, 됐으면 `curator_transitions.py`를 **인라인 자동 실행**: 마지막 활동(use/view/patch) 기준 **30일 미사용→stale, 90일→archive**(`.archive/` 로 이동, 삭제 아님). 변경 전 tar.gz 스냅샷을 뜨고, 다시 쓰이면 stale→active로 재활성화. **pin된 스킬과 사용자 작성(`created_by:user`) 스킬은 절대 건드리지 않음.** 의미 기반 중복 통합은 v0.17.0부터 백그라운드 자동 실행(아래 AUTO-CURATE), 수동 실행은 `/curate-skills`(병합 시 `absorbed_into` 기록). 수동 제어 커맨드: `/curator-status`(상태·통계), `/prune-skills`(N일 미사용 일괄, dry-run), `/archive-skill`(단일), `/pin-skill`(보호), `/restore-skill`(복구), `/curator-rollback`(스냅샷 전체 롤백 — usage 메타 포함, 롤백도 언두 가능).
- **AUTO-CURATE** (v0.17.0) — **의미 기반 통합도 백그라운드 자동 실행**. 지금까지 큐레이션은 두 층으로 갈라져 있었습니다: 시간 기반 전이(stale/archive)는 자동, 의미 기반 통합은 사용자가 `/curate-skills`를 직접 쳐야 하는 수동. 후자는 판단이 필요해 훅에서 인라인으로 돌 수 없었기 때문인데, 백그라운드 증류가 쓰는 `claude -p` 인프라를 그대로 재사용하면 됩니다. 큐레이션 주기가 됐을 때 `SessionStart` 훅이 시간 전이를 인라인 실행한 뒤, **통합 패스를 같은 큐에 `trigger=curate` 잡으로 얹습니다**(같은 baseline 스냅샷·롤백·가드를 그대로 받음). 결과는 다음 세션 시작 시 보고되고 `/curator-rollback` 으로 통째로 되돌릴 수 있습니다. 잡은 UTC 하루 1건으로 dedupe되어 세션을 여러 개 동시에 열어도 자식이 스킬 트리를 두고 경합하지 않습니다. 끄려면 `SIS_AUTO_CURATE=0`. 모델은 고정하지 않습니다 — 증류와 마찬가지로 계정이 현재 쓰는 모델을 그대로 물려받고, 다르게 쓰고 싶을 때만 `SIS_CURATE_MODEL` 로 지정합니다. 무인 실행이라 프롬프트는 보수적으로 지시합니다 — 서로를 인용하거나 한 작업의 여러 단면인 클러스터만 병합하고, 도메인만 겹치는 것은 넘어가며, 합계가 검증기 상한(100,000자)에 근접하는 묶음은 시도하지 않습니다. **아카이브 후 살아있는 스킬이 아카이브된 이름을 참조하고 있는지 grep으로 훑는 단계가 필수**입니다(첫 실전 패스에서 실제로 dangling 참조가 발생한 실패 모드).
- **수동 트리거** — `/distill-skill` 로 언제든 증류를 직접 실행.
- **MIGRATION** (v0.11.0) — 플러그인/마켓플레이스 **리네임 마이그레이션**. 2026-07 개편(`samton-claude`→`samton-plugins`, `self-improving-skills`→`claude-code-self-improving-skills` 등 4종)으로 orphan된 로컬 상태를 `/migration` 한 번으로 최신 이름에 올립니다: `~/.claude/settings.json`(permissions.allow 네임스페이스·enabledPlugins 키·stale marketplace URL), 학습 스킬 본문의 구 네임스페이스/경로 참조, `~/.codex/config.toml`, codex 상태 디렉토리·provenance. 기본 dry-run → 확인 후 `--apply`(파일별 백업, 멱등). rename 맵은 `scripts/migrate_local.py`에 데이터로 유지되어 향후 리네임에도 확장 가능. `provenance: self-improving-skills` 마커는 의도적으로 유지되는 값이라 건드리지 않습니다.
- **HERMES SYNC** (v0.10.0) — Hermes 최신(v2026.7.1, 2026-07) 대조 재이식 14건. **distiller 프롬프트**: 이번 세션에 로드된(in-play) 스킬 최우선 패치 + 사용자 교정·좌절 표현을 1급 시그널로(태스크 결부 교정만 스킬에, 일반 선호는 네이티브 메모리 몫) + 세션 내 해소된 일시 오류 캡처 금지 + description 저장 전 자기검증 + "실행/관찰한 것만 기록" 환각 방지. **트리거**: 파일 편집 0회 조사·디버깅 구간도 증류 대상(`SIS_DISTILL_READONLY_THRESHOLD`). **큐레이터 안전장치**: `archive_one` fail-closed 가드(absorbed_into 실존·자기참조 검증, pinned/user 거부 — Hermes #29912 이식), pinned 스킬 자율 편집 자동 롤백, 타임스탬프 접미 아카이브 정확 복구(prefix 삼킴 금지 — Hermes 992b9223), 스냅샷에 usage 메타 수록 + `/curator-rollback`(롤백 자체도 언두 가능 — fc1119ca), `/curate-skills` 시각 기록을 검토 시작 시점으로 이동(`mark-curated`, nag 루프 차단) + 구조화 결과 기록(consolidations.yaml). **현대화**: SessionStart 상시 주입 축소(~10줄→~4줄, 권한 복구는 차단 발생 지점으로 이동), `SIS_DISTILLER_MODEL` 비용 라우팅 opt-in.
- **UPSTREAM 기여** (v0.6.0) — 플러그인 *자기 자신*의 개선을 닫는 두 단계. **L1(감지·알림)**: `Stop` 훅이 이번 구간에서 claude-code-self-improving-skills 코어 소스(`plugins/claude-code-self-improving-skills/**`)를 편집했음을 감지하면, skills 증류와 별개로 "코어를 건드렸으니 upstream에 PR로 제안 가능"을 알립니다(정보 제공만, 자동 행동 없음). **L2(opt-in 자동 PR)**: `/propose-plugin-improvement` 가 격리된 fresh clone에서 변경을 재현해 upstream(`samton-inc/samton-plugins`)에 PR을 엽니다. **설계 불변식**: 자동 push·머지 없음(PR까지만, 머지는 사람) / `SIS_PLUGIN_PR=1` opt-in 기본 OFF / fresh clone + commit 전 index 초기화 + 화이트리스트 서브트리만 스테이징(그 밖 경로가 staged되면 PR 중단)으로 transcript·로컬 비밀 유입 차단 / write 권한 없으면 fork 경유. distiller(skills 전담)와 책임이 분리되어 있습니다 — distiller는 PR을 만들지 않습니다.

## 흐름

```
복잡한 작업 종료
   │  Stop 훅: 마지막 증류 이후 도구호출 ≥ SIS_DISTILL_THRESHOLD 이고 파일편집 ≥ SIS_MIN_FILE_EDITS?
   ▼ (yes)
종료를 1회 block → "skill-distiller에 위임하라"
   ▼
skill-distiller 서브에이전트 (격리 컨텍스트)
   │  기존 스킬과 매칭 → patch  /  없으면 class-level 신규  /  일회성이면 skip
   ▼
~/.claude/skills/<name>/SKILL.md 작성·수정
   │  PostToolUse 훅: frontmatter 검증 + provenance 스탬프
   ▼
다음 세션 시작 → Claude Code가 자동 발견 → 스킬 재사용
```

## 설정 (환경변수)

| 변수 | 기본값 | 의미 |
|---|---|---|
| `SIS_REVIEW_MODE` | `background` | `background`(별도 프로세스, 메인 턴 무출력·무과금) / `foreground`(기존 nudge) / `off`. background 에서 CLI 미인증·미발견이면 자동으로 foreground 폴백 |
| `SIS_CLAUDE_BIN` | 자동탐색 | `claude` 절대경로. GUI 로 뜬 훅은 PATH 에 `~/.local/bin` 이 없을 수 있어 필요할 때가 있음 |
| `SIS_CORE_TOUCH_MIN_CALLS` | `6` | 코어 소스 편집(L1 권고)이 백그라운드 증류를 유발하려면 필요한 최소 도구 호출 수. 이게 없으면 이 리포에서 한 줄만 고쳐도 매 턴 세션이 뜸 |
| `SIS_STATE_DIR` | `~/.claude/self-improve` | 큐·백업·텔레메트리를 전부 옮김 |
| `SIS_DISTILL_THRESHOLD` | `12` | 증류 nudge를 띄울, 마지막 증류 이후 누적 도구 호출 수 |
| `SIS_MIN_FILE_EDITS` | `2` | nudge 조건: 마지막 증류 이후 실제 파일 편집(Edit/Write/MultiEdit) 최소 횟수. 순수 탐색·질의 턴은 트리거하지 않게 함 |
| `SIS_DISTILL_READONLY_THRESHOLD` | `24` | 파일 편집이 **0회**인 구간도 도구 호출이 이 수를 넘으면 nudge — 긴 조사·디버깅 세션의 진단 기법(커맨드 사다리·원인 규명 패턴)이 영원히 증류되지 않는 갭을 막음 (Hermes 는 툴 iteration 만으로 트리거) |
| `SIS_DISTILLER_MODEL` | (없음) | 증류를 **계정 기본 모델이 아닌 특정 티어**로 돌리고 싶을 때만 지정(예: `sonnet`). 미설정이면 자식에 `--model` 을 넘기지 않아 계정이 지금 쓰는 모델을 그대로 물려받습니다. foreground 모드에서는 nudge·/distill-skill 이 distiller 호출에 `model="<값>"` 을 포함하라고 안내하는 용도로도 쓰입니다(`haiku` 값은 무시 — 서브에이전트 Haiku 금지 정책) |
| `SIS_CURATE_MIN_SKILLS` | `8` | 자동 큐레이션을 시작하는 학습 스킬 수 |
| `SIS_CURATE_INTERVAL_DAYS` | `7` | 큐레이터 자동 실행 간격(일) |
| `SIS_AUTO_CURATE` | (없음) | `0` 으로 설정하면 **의미 기반 통합 패스의 백그라운드 자동 실행을 끔**(시간 기반 stale/archive 전이는 계속 동작). 끄면 SessionStart 가 `/curate-skills` 수동 실행을 안내 |
| `SIS_CURATE_MODEL` | (없음) | 통합 패스를 **계정 기본 모델이 아닌 특정 티어**로 돌리고 싶을 때만 지정. 미설정이면 증류와 동일하게 계정 모델을 물려받음 |
| `SIS_STALE_AFTER_DAYS` | `30` | 마지막 활동 후 이 일수 미사용 시 stale 마킹 |
| `SIS_ARCHIVE_AFTER_DAYS` | `90` | 마지막 활동 후 이 일수 미사용 시 `.archive/` 로 이동 |
| `SIS_PLUGIN_PR` | (없음) | `1` 로 설정하면 `/propose-plugin-improvement` 의 L2 자동 PR을 활성화. 미설정이면 코어 변경 L1 알림만 동작하고 PR은 만들지 않음 |
| `SIS_RUNTIME` | (자동 판별) | `local` 이면 이 플러그인이 동작, `cowork` 이면 모든 훅이 조용히 통과 (아래 참고) |

`~/.claude/settings.json` 의 `env` 블록이나 셸 환경에서 조정합니다.

## Cowork 변형과의 공존

같은 계정에 `claude-cowork-self-improving-skills` 를 등록해두면 그 플러그인이 **로컬 Claude Code 세션에도 동기화되어 내려옵니다**. 두 플러그인은 같은 훅 이벤트를 쓰므로, 아무 장치가 없으면 한 번의 Stop 에 둘 다 반응합니다.

`scripts/runtime_env.py` 가 각 훅 진입점에서 환경을 판별해, 자기 환경이 아니면 즉시 통과합니다. 판별 신호는 하나입니다: **HOME 이 `local-agent-mode-sessions` 경로 세그먼트 아래**면 Cowork (세션마다 만들어지는 샌드박스 홈), 아니면 로컬.

초기 구현은 `~/.claude/plugins/installed_plugins.json` 부재도 Cowork 신호로 썼다가 제거했습니다. **계정에서 동기화된 플러그인은 그 파일에 기록되지 않으므로**, 계정 등록분만 쓰고 CLI 설치를 한 번도 안 한 사용자는 멀쩡한 로컬 머신에 그 파일이 없습니다. 그 경우 이 플러그인이 조용히 물러나 증류 루프가 통째로 죽었습니다 — 훅 래퍼가 stderr 를 버리고, 안내를 띄울 변형이 바로 그 침묵한 변형이라 아무 신호도 남지 않습니다. 파일이 없다는 사실은 환경의 증거가 아닙니다.

대가로 샌드박스 경로 밖에서 도는 Cowork 컨테이너는 로컬로 읽힙니다. 그런 환경은 아직 관측되지 않았고, 그 실패(휘발될 홈에 스킬을 증류)가 진짜 로컬에서 루프를 죽이는 것보다 가볍습니다. 해당하면 `SIS_RUNTIME` 으로 고정하세요.

훅 스크립트 이름이 Cowork 변형과 다른 것도 같은 맥락입니다 — `${CLAUDE_PLUGIN_ROOT}` 는 실행 시점에야 치환되므로, 이름이 같으면 등록 문자열이 일치해 한쪽 훅이 조용히 사라집니다. CI 가 두 사본의 동일성과 훅 이름 충돌을 모두 검사합니다.

## 백그라운드 증류 설정 (기본 모드)

`SIS_REVIEW_MODE` 기본값은 `background` 입니다. Stop 훅이 큐에 좌표만 넣고 즉시 끝나고, 별도 프로세스가 `claude -p` 로 증류합니다 — **메인 대화에는 출력도 과금도 없습니다.**

동작하려면 **CLI 인증을 1회 붙여야 합니다.** 백그라운드 워커는 로그인할 터미널이 없기 때문입니다.

```bash
claude setup-token
```

발급된 토큰을 파일에 넣습니다(채팅·커밋에 붙여넣지 마세요):

```bash
install -m 600 /dev/null ~/.claude/self-improve/worker.env
# 편집기로 열어 아래 한 줄 추가
# CLAUDE_CODE_OAUTH_TOKEN=<발급받은 토큰>
```

워커는 이 파일을 `O_NOFOLLOW` + 일반파일 확인 후 읽어 **자식 프로세스 환경에만** 전달하고, 로그·큐·작업 기록 어디에도 남기지 않습니다.

> **주의 — 이 토큰은 자식으로부터 하드하게 보호되지 않습니다.** 예전에는 deny 규칙이 자식의 `Read`·`Glob`·`Grep` 을 이 경로에서 막았지만, [보안 모델](#보안-모델--정직하게) 에 적은 대로 deny 규칙은 제거됐습니다. 지금 남은 것은 "스킬 트리 밖 자격증명을 읽지 말라"는 프롬프트 지시뿐이고, 그 지시는 인젝션과 같은 채널에 있습니다. 이 상태가 불편하면 `SIS_REVIEW_MODE=foreground` 로 두면 토큰 자체가 필요 없습니다.

> **Windows 주의**: 권한 검사는 POSIX 모드 비트 기준이라 macOS·Linux 에서만 강제됩니다. Windows 에서는 ACL 을 확인하지 않으므로, 이 파일의 접근 권한은 사용자가 직접 제한해야 합니다.

상태 확인은 슬래시 커맨드로 하세요 — 플러그인 캐시 경로를 직접 쓸 필요가 없고 세 OS 에서 모두 동작합니다:

```
/distill-status
```

막힌 작업을 다시 돌리려면 `/distill-status retry` 입니다.

인증 전이거나 `claude` 를 못 찾으면 **기존 nudge 방식(foreground)으로 자동 폴백**하므로 루프가 죽지는 않습니다. `SIS_REVIEW_MODE=foreground` 로 명시 고정하거나 `off` 로 끌 수 있습니다.

## 플랫폼 요구사항

훅은 bash 스크립트(`hooks/*.sh`)이고, 스크립트는 Python 3 로 돕니다(CI 는 3.11, 실측은 3.14 에서 확인).

| OS | 필요 | 비고 |
|---|---|---|
| macOS · Linux | Python 3 (`python3`) | 기본 동작. 별도 준비 없음 |
| **Windows** | **Git for Windows (Git Bash)** + Python 3 | Claude Code 는 Windows 에서 훅을 Git Bash 로 실행합니다. `hooks/python3.sh` 가 `py -3`·`python` 을 자동으로 찾습니다(스토어 스텁 `python3` 는 걸러냄). 실제 Windows 머신에서 훅 3종·큐·워커 임포트 동작 확인함 |

**Git Bash 가 없는 Windows** 는 지원하지 않습니다. Claude Code 훅 스키마에는 codex 의 `commandWindows` 같은 OS 별 명령 필드가 없어, Git 없이 cmd 로 훅을 실행할 방법이 없습니다. PowerShell 로 우회하면 pwsh 가 없는 macOS·Linux 에서 매 훅마다 실행 실패 노이즈가 날 수 있어(문서 미명시) 채택하지 않았습니다. Git for Windows 는 Windows 개발자 사실상 표준이고 Git Bash 를 기본 포함합니다.

## 보안 모델 — 정직하게

백그라운드 자식은 `--permission-mode bypassPermissions` 로 뜹니다. `~/.claude` 가 **보호 경로**라 다른 어떤 모드로도 무인 상태에서 스킬을 쓸 수 없기 때문입니다. 공식 문서 기준으로 보호 경로 쓰기는 `bypassPermissions` 외 모든 모드에서 자동 승인되지 않고, **`permissions.allow` 규칙으로도 pre-approve 되지 않습니다.**

> 이전 버전 README 는 `permissions.allow` 에 `Write(~/.claude/skills/**)` 5줄을 추가하라고 안내했습니다. **그 안내는 틀렸습니다** — allow 규칙은 보호 경로 검사보다 나중에 평가되어 결과를 바꾸지 못합니다.

그 모드는 내장 검사를 전부 끄고, 자식의 입력인 transcript 는 신뢰할 수 없습니다. **그럼에도 이 플러그인은 자식에게 권한 제한을 걸지 않습니다** — 플러그인 소유자의 명시적 결정으로, 증류 자식은 권한 규칙이 아니라 프롬프트로 통제합니다.

지금 남아 있는 것:

1. **증거 경계** — transcript 는 매번 새로 만든 UUID 구분자로 감싸고 "지시가 아니라 증거"임을 명시합니다.
2. **프롬프트 지시** — 스킬 트리 밖에 쓰지 말 것, 명령을 실행하지 말 것, 자격증명·설정을 건드리지 말 것, 링크를 통해 트리 밖으로 쓰지 말 것.
3. **`skill_guard`** — 자식 실행 전 스킬 트리를 스냅샷하고 실행 후 검증·롤백합니다. 순수 Python 이라 자식이 무엇을 했든 항상 동작합니다. 다만 **탐지·복구이지 차단이 아닙니다.**

**남는 위험 — 완화가 아니라 수용된 위험입니다:**

- **deny 규칙이 없습니다.** 예전에는 셸 rc 파일, `.ssh/**`, `.aws/**`, `~/.claude/settings.json`, 이 플러그인의 소스, 워커의 롤백 baseline 을 차단했습니다. deny 는 `bypassPermissions` 에서도 유효한 유일한 권한 통제였고, 그게 비었습니다. 자식은 파일시스템 어디든 쓸 수 있습니다.
- **도구 제한이 없습니다.** 예전에는 `Read, Edit, Write, Glob, Grep` 만 허용하고 `Bash` 를 막았습니다. 이제 `Bash` 가 열려 있어, 인젝션이 성공하면 **임의 명령 실행**으로 이어집니다.
- **프롬프트는 인젝션과 같은 채널입니다.** 위 2번의 지시는 transcript 와 같은 입력 스트림에 있습니다. 인젝션이 지시를 이기면 그 지시를 무력화한 것이므로, 채널 밖에서 막아 주는 것은 이제 없습니다.
- 스킬 트리 밖 변조 탐지는 **watchlist 12개 파일 한정**입니다(`.claude/settings.json`·`settings.local.json`·`CLAUDE.md`, 셸 rc 6종, `.envrc`, `.npmrc`, `.gitconfig`). 전체 파일시스템 스냅샷은 불가능하고, 이제 이 watchlist 가 실질적으로 유일한 사후 신호입니다. `~/.claude.json` 은 자식이 정상 동작으로 다시 쓰기 때문에 목록에서 빠져 있는데, deny 도 없어졌으므로 그 파일은 이제 막히지도 관측되지도 않습니다.
- **심볼릭 링크된 스킬은 롤백 대상이 아닙니다.** 링크를 따라가면 임의 파일이 스냅샷에 딸려 들어오거나 순환이 생기므로 스냅샷은 링크에서 멈춥니다. 링크를 통한 쓰기는 되돌려지지도 위반으로 보고되지도 않습니다. 어떤 링크가 보호 밖인지는 `/distill-status` 가 호출 시점의 트리를 훑어 알려줍니다. 예전에는 링크가 하나라도 있으면 **전체 작업을 거부**했지만, 링크 하나가 라이브러리 전체의 증류를 막는 대가가 너무 컸습니다.
- 관리형 조직이 `permissions.disableBypassPermissionsMode` 를 걸었거나 root 로 실행 중이면 백그라운드 모드는 동작하지 않고 foreground 로 폴백합니다.

**이 트레이드오프를 받아들일 수 없다면 `SIS_REVIEW_MODE=foreground` 로 두세요.** 사용자가 보는 앞에서 서브에이전트가 증류하므로 이상한 동작을 즉시 알아챌 수 있습니다. 대신 메인 턴에 과금됩니다. 무인 실행의 위 위험은 `background` 모드에만 해당합니다.

## 설계 원칙 — dev-log의 실패에서 배운 가드레일

이 플러그인은 같은 마켓플레이스의 `dev-log` 훅이 **396개 실제 세션에서 단 한 번도 발동하지 않은** 원인 분석 위에 만들어졌습니다. 그 7대 실수를 모두 막았습니다:

1. **transcript를 실측 구조로 파싱.** 도구 호출은 `"name":"Edit"` (assistant 행의 `message.content[]` 안)으로 기록됩니다 — `"tool":"Edit"`가 아닙니다. `analyze_turn.py`는 실제 구조를 파싱합니다.
2. **"이미 했나"를 실제 행위로 판정.** 플러그인 이름 문자열(`grep -q`)이 아니라, skill-distiller로의 Task 위임 또는 `~/.claude/skills`의 SKILL.md 쓰기라는 실제 tool_use를 anchor로 봅니다. (substring 매칭은 플러그인 자기 이름이 transcript에 박혀 자기트립합니다.)
3. **임계값을 실데이터로.** 키워드 5회 같은 비현실적 게이트 대신, 누적 도구 호출·파일 편집 수라는 작업량 신호를 씁니다.
4. **Stop-hook 계약 준수.** block은 STDOUT + exit 0 의 `{"decision":"block","reason":...}` 로 emit합니다 (stderr+exit2 아님). `stop_hook_active` 루프 가드 포함.
5. **작업 유형 무관 트리거.** "빌드 에러 수정"처럼 좁은 조건(사용자 396세션 중 빌드 도구 호출 12회뿐)이 아니라, 모든 작업에 걸리는 기계적 복잡도 신호를 씁니다.
6. **situation-first 스킬 description.** distiller가 쓰는 SKILL.md는 방어적 "MUST ALWAYS"가 아니라 "이런 상황에 사용한다"는 상황 매칭으로 작성합니다.
7. **모델 자발성에만 의존하지 않음.** 훅이 강제하되, 그 훅이 정확하게 동작합니다. 동시에 어떤 에러에도 fail-safe로 approve(세션을 막지 않음).

## 한계 (정직하게)

- Hermes의 **무음 데몬 스레드**는 v0.13.0 의 백그라운드 모드로 대응물이 생겼습니다 — 메인 턴에 출력도 과금도 없습니다. 다만 **무료는 아닙니다**: 별도 `claude -p` 세션이므로 구독 사용량을 소모합니다. v0.17.0 에서 **지출 상한 두 개를 모두 제거**했습니다. 둘 다 보호 장치처럼 보였지만 실제로는 반대로 동작했습니다.

- **잡당 상한**(`SIS_DISTILL_MAX_USD`, 기본 0.50): evidence 창이 최대 20만 자라 자식이 **자기 프롬프트를 읽는 도중에** 천장을 넘었습니다(105행 트랜스크립트에서 2턴 만에 $1.15, 반면 정상 완주는 9턴 $1.67 — 둘 다 실측). 어떤 정상 실행도 그 아래 머물 수 없는 천장은 보호가 아니라, **작업만 중단시키고 사용량은 그대로 쓰는** 장치입니다. 게다가 실제 CLI 는 예산 중단 시 **non-zero 로 종료**하는데 워커의 차단 로직은 파싱 경로에만 있어 도달하지 못했고, 결과적으로 잡마다 3회씩 재시도하며 같은 자리에서 끊겼습니다(실측: 11잡 × 3회, 산출물 0). 이 갭 자체도 함께 막았습니다 — non-zero 종료에서도 봉투의 `subtype`/`terminal_reason` 을 읽어 예산 중단이면 차단합니다.
  - **달러 표기 주의**: 자식은 부모 세션의 OAuth 로그인을 상속하므로, claude.ai 구독에서는 위 금액이 **청구액이 아니라 API 요율 환산치**이고 실제로 줄어드는 것은 플랜 사용량입니다. 즉 이 상한은 **사용자가 지불하지도 않는 단위로 매겨진 천장**이었습니다. `ANTHROPIC_API_KEY` 로 종량 과금하는 환경에서는 진짜 비용이므로, 그런 경우엔 CLI 자체 설정으로 천장을 두면 됩니다(플러그인은 더 이상 넘기지 않습니다).
- **하루 상한**(`SIS_DISTILL_MAX_JOBS_PER_DAY`, 기본 12건): 성공이 아니라 **시도**를 세는 장치라, 잡이 전부 실패한 날에도 할당량이 차서 그다음 정상 작업을 조용히 거부했습니다.

남은 천장은 **wall-clock 타임아웃**(`COMMAND_TIMEOUT_SECONDS`, 600초)과 트리거 자체(임계 + 편집 하한, 구간당 1회)입니다.

같은 판단으로 **모델 고정도 걷어냈습니다**(v0.17.0). 전에는 `--model sonnet` 이 박혀 있어, 사용자가 본인 작업에 어떤 티어를 쓰고 있든 증류만 조용히 다른 모델로 내려갔습니다. 증류는 사용자를 대신해 판단하는 일이라 티어 선택도 사용자 몫이어야 합니다. 이제 `--model` 을 아예 넘기지 않아 **계정이 현재 쓰는 모델을 그대로 물려받습니다** — 실측으로 Opus 5 계정에서 자식이 `claude-opus-5[1m]` 로 떴습니다(`--setting-sources ""` 는 설정 *파일* 로드를 막을 뿐, 계정의 모델 선택까지 끊지는 않습니다). 특정 티어로 고정하고 싶으면 `SIS_DISTILLER_MODEL` / `SIS_CURATE_MODEL` 을 쓰면 됩니다.
- 증거는 **메인 transcript 만** 읽습니다. 서브에이전트 작업은 `subagents/` 하위 별도 파일이라 증류 근거에 포함되지 않습니다.
- 프리픽스 캐시 상속, 런타임 ContextVar 기반 provenance도 이식 불가 → frontmatter 스탬프로 근사.
- 크로스세션 FTS5 검색(Hermes의 RECALL)은 이 플러그인 범위 밖입니다. 필요하면 `remember` 플러그인(메모리 자율 캡처)과 함께 쓰는 것을 권장.
- **메모리 루프(MEMORY.md/USER.md)는 의도적으로 만들지 않습니다.** Hermes는 메모리(서술적 지식)와 스킬(절차적 지식)을 한 루프에 묶었지만, Claude Code는 **네이티브 `MEMORY.md` auto memory**(v2.1.59+ GA, 기본 ON)가 이미 에이전트 자율 메모리를 담당합니다. 이 플러그인은 **절차적 능력(스킬)** 축만 맡고, 사실 메모리는 네이티브에 위임 — 중복·이중 주입을 피합니다. (역할 분담: `CLAUDE.md`=정적 정책, 네이티브 `MEMORY.md`=자율 사실 메모리, `remember`=세션 요약, 이 플러그인=재사용 스킬.)

## 구성 파일

```
claude-code-self-improving-skills/
├── hooks/
│   ├── hooks.json            # Stop + SessionStart + PreToolUse + PostToolUse 등록
│   ├── distill-nudge.sh      # Stop 래퍼 (fail-safe)
│   ├── session-init.sh       # SessionStart 래퍼
│   ├── backup-skill.sh       # PreToolUse 래퍼 (편집 직전 백업)
│   └── validate-skill.sh     # PostToolUse 래퍼 (검증 + 롤백)
├── scripts/
│   ├── analyze_turn.py        # 복잡도 측정 + block/approve 결정 + usage 캡처 + 코어 변경 감지(L1)
│   ├── usage_store.py         # 스킬 사용 telemetry 저장소 (atomic+flock, _meta prune)
│   ├── curator_transitions.py # 시간기반 stale→archive 상태머신 (+restore/prune, use_count 보호)
│   ├── curator_backup.py      # 변경 전 tar.gz 스냅샷
│   ├── backup_skill.py        # PreToolUse: SKILL.md 편집 직전 백업
│   ├── session_init.py        # 자기개선 안내 + 큐레이터 자동 실행
│   ├── validate_skill.py      # SKILL.md 검증 + 롤백 + provenance + patch 집계
│   ├── propose_pr.py          # 범용 PR plumbing (fresh clone, 화이트리스트, fork 경유)
│   └── propose_plugin_pr.py   # 코어 기여 어댑터 (SIS_PLUGIN_PR 게이트)
├── agents/
│   └── skill-distiller.md     # 격리 리뷰어 (patch>create 우선순위)
├── tests/                     # pytest 스위트 (uv run --with pytest -- pytest tests/)
└── commands/
    ├── distill-skill.md       # 수동 증류 트리거
    ├── curate-skills.md       # umbrella 통합 패스 (LLM, absorbed_into 기록)
    ├── curator-status.md      # 루프 상태·사용 통계 조회
    ├── prune-skills.md        # N일 미사용 일괄 아카이브 (dry-run)
    ├── archive-skill.md       # 단일 스킬 수동 아카이브
    ├── pin-skill.md           # 스킬 pin (자동 정리 보호)
    ├── restore-skill.md       # 아카이브 복구
    └── propose-plugin-improvement.md  # 코어 개선을 upstream에 PR 제안 (L2, opt-in)
```

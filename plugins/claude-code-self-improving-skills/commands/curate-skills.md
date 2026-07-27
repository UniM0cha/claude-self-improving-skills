---
description: 학습된 스킬 라이브러리(~/.claude/skills)를 정리한다 — umbrella 통합, 오래된 스킬 아카이브
---

학습된 스킬 라이브러리를 검토하고 정리하세요. 이것은 자기개선 루프의 **유지보수 단계**이며, 단순 중복 탐지가 아니라 **umbrella-building 통합 패스**입니다.

## 철학 (Hermes 큐레이터 이식)

스킬 라이브러리의 목표는 **class-level 지침과 경험 지식의 도서관**입니다. 한 세션의 특정 버그 하나씩을 담은 좁은 스킬 수백 개의 평평한 더미는 도서관의 **실패**입니다. 에이전트는 스킬을 이름이 아니라 description으로 매칭하므로, 라벨 달린 하위 섹션을 가진 넓은 umbrella 스킬 1개가 좁은 형제 스킬 5개보다 발견성이 좋습니다 — 그 반대가 아닙니다.

올바른 목표 형태: **풍부한 본문 + `references/`·`templates/`·`scripts/` 지원 파일을 가진 class-level 스킬**. 세션 하나당 스킬 하나의 마이크로 항목이 아닙니다.

## 절차

### 1. 학습 스킬 + 사용 통계 수집

```bash
ls -d ~/.claude/skills/*/ 2>/dev/null
grep -rl "provenance: self-improving-skills" ~/.claude/skills --include=SKILL.md 2>/dev/null
echo "=== usage telemetry ==="
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/usage_store.py" dump 2>/dev/null
```

각 학습 스킬의 `name`·`description`과 usage 통계(use/view/patch, 마지막 사용, state, created_by)를 표로 만드세요.

데이터 수집이 끝나면 **검토를 시작하는 이 시점에** 큐레이션 시각을 기록하세요 — 패스가 중간에 중단되거나 "변경 없음"으로 끝나도 기록이 남아, SessionStart 큐레이터가 매 세션 재트리거(nag)하지 않습니다:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/curator_transitions.py" mark-curated
```

### 2. Hard rules — 위반 금지

1. **`created_by: agent`인 스킬만 건드립니다.** 사용자 작성(user)·타 플러그인 스킬은 절대 수정·아카이브·통합하지 않습니다.
2. **삭제 금지.** 아카이브(`.archive/`로 이동, `/restore-skill`로 복구 가능)가 최대 파괴 행위입니다.
3. **`pinned: true` 스킬은 통째로 스킵합니다.**
4. **use_count를 통합 회피나 아카이브의 근거로 쓰지 마세요.** 카운터는 최근 도입됐고 대부분 0입니다. `use=0`은 "가치 없음"의 증거가 아니라 **증거의 부재**입니다. 겹침 판단은 빈도가 아니라 **내용**으로 하세요. (미사용 기반 정리는 시간 기반 상태머신이 이미 담당합니다 — 이 패스의 일이 아닙니다.) 단, 통합 시 "어느 쪽을 기준으로 삼을지"의 참고로는 쓰세요.
5. **"각 스킬의 트리거가 서로 다르다"는 이유로 통합을 거부하지 마세요.** 쌍별 구별성은 잘못된 기준입니다. 올바른 기준: **"사람 유지보수자라면 이것을 N개의 분리된 스킬로 쓰겠는가, 라벨 달린 N개 하위 섹션을 가진 1개 스킬로 쓰겠는가?"** 후자라면 통합하세요.

### 3. 작업 방법

1. 전체 후보 목록에서 **prefix/도메인 클러스터**를 식별하세요 (첫 단어나 도메인 키워드를 공유하는 스킬들).
2. 2개 이상 멤버가 있는 각 클러스터에 대해 "이 스킬들이 공통으로 섬기는 **umbrella class**는 무엇인가?"를 물으세요.
3. 클러스터별로 세 가지 통합 방식 중 맞는 것을 쓰세요:
   - **a. 기존 umbrella에 병합** — 클러스터 중 하나가 이미 충분히 넓으면, 거기에 각 형제의 고유 통찰을 라벨 섹션으로 추가하고 형제들을 아카이브.
   - **b. 새 umbrella 생성** — 충분히 넓은 멤버가 없으면 공유 워크플로우를 다루는 class-level 스킬을 새로 작성하고 흡수된 형제들을 아카이브.
   - **c. references/templates/scripts로 강등** — 좁지만 가치 있는 세션-특정 내용은 umbrella의 `references/<topic>.md`(상세 지식·재현 레시피), `templates/`(복사해 쓰는 시작 파일), `scripts/`(재실행 가능한 검증 스크립트)로 이동 후 원본 아카이브.
4. 흡수 아카이브는 반드시 umbrella 이름을 함께 기록하세요 (`absorbed_into` — 폐기가 아니라 통합이었음이 남습니다):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/curator_transitions.py" archive "<흡수된-스킬>" "<umbrella-스킬>"
   ```

   명령이 `ok: false`를 반환하면 그대로 따르세요: `umbrella not found`면 **umbrella 스킬을 먼저 만들거나 patch한 뒤 재시도**하고, pinned·user 거부는 정상 보호 동작입니다(`--force`는 사람이 직접 결정할 때만).

5. 통합 후 umbrella의 `description`이 합쳐진 범위를 정확히 반영하도록 갱신하세요.

### 4. 패키지 무결성 — 선택이 아님

강등·아카이브 전에 스킬을 SKILL.md만이 아니라 **완전한 디렉토리 패키지**로 검사하세요. 원본에 `references/`·`templates/`·`scripts/`·`assets/`가 있거나 SKILL.md가 그런 상대 링크를 포함하면, SKILL.md만 떼어 `<umbrella>/references/<old>.md`로 평탄화하지 마세요. 안전한 경로는 셋뿐입니다:
- 독립 스킬로 유지, 또는
- 필요한 지원 파일 전부를 umbrella의 해당 디렉토리로 재배치하고 본문 경로를 전부 고쳐 **완전 병합**, 또는
- 원본 패키지를 통째로 아카이브.

이동된 파일을 가리키는 깨진 링크를 절대 남기지 마세요.

### 5. description 압축

학습 스킬의 description은 **모든 세션의 시스템 프롬프트에 실립니다**. 500자를 넘는 description은 트리거 문구를 보존하면서 압축 후보로 검토하세요 (구체적 트리거 문구·상황 매칭은 유지 — 그것이 트리거 정확도의 핵심입니다).

### 6. 2단계 실행: 계획 → 승인 → 적용

**먼저 변경 계획만 리포트로 제시하세요**: 클러스터별 통합 방식, 흡수될 스킬, 아카이브 대상, 압축할 description. **사용자 승인 후에만** 실제 편집·아카이브를 수행하세요.

### 7. 구조화 결과 기록 + 보고

적용이 끝나면 이번 패스의 결과를 구조화 파일로 남기세요 — **변경이 없었어도 빈 리스트로 기록합니다** (Hermes 큐레이터의 필수 consolidations/prunings 블록 이식):

`~/.claude/self-improve/logs/curator/<UTC타임스탬프>/consolidations.yaml`

```yaml
consolidations:            # umbrella로 흡수한 통합
  - from: <흡수된-스킬>
    into: <umbrella-스킬>
    reason: <한 줄>
prunings:                  # 통합 없는 단순 아카이브
  - name: <스킬>
    reason: <한 줄>
```

기록 후 대조하세요: **이번 패스에서 `.archive/`로 이동한 모든 스킬이 정확히 위 두 리스트 중 하나에 있어야 합니다.** 리스트에 없는 이동이 있으면 리포트에 명시하세요.

통합한 클러스터, 아카이브한 스킬, 그대로 둔 스킬 수를 요약하세요. 변경이 없었다면 "정리할 것이 없었습니다"로 끝내세요. (큐레이션 시각은 절차 1의 `mark-curated`가 이미 기록했습니다 — 여기서 다시 기록하지 마세요.)

잘못 정리했다면 전체 되돌리기가 가능합니다: `/curator-rollback` (변경 직전 tar.gz 스냅샷 + usage 메타 복원).

$ARGUMENTS 가 주어지면 그 범위(예: 특정 도메인만)에 집중하세요.

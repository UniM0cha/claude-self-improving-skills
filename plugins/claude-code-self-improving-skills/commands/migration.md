---
description: 플러그인/마켓플레이스 리네임(samton-plugins 개편)으로 orphan된 로컬 설정을 최신 이름으로 마이그레이션한다
---

플러그인·마켓플레이스 이름이 바뀐 뒤(예: 2026-07 `samton-claude`→`samton-plugins`,
`self-improving-skills`→`claude-code-self-improving-skills`) 로컬에 남은 구-이름 참조를
한 번에 최신으로 올리는 커맨드입니다. 다음 순서로 진행하세요:

## 1. Dry-run으로 orphan 목록 확인

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/migrate_local.py"
```

출력을 사용자에게 그대로 보여 주세요. 대상: `~/.claude/settings.json`(permissions.allow
네임스페이스·enabledPlugins 키·extraKnownMarketplaces stale URL), `~/.claude/skills`의
학습 스킬 본문 참조, `~/.codex/config.toml`, codex 상태 디렉토리(`~/.codex-self-improvement`
→ `~/.self-improving-skills`)와 codex 증류 스킬 provenance, 그리고 팀 공유 기능
제거(v0.12.0)에 따른 `skill_usage.json`의 `created_by: team` → `agent` 전환.

`team_sync.json`·`team_config.json`·`team_quarantine/` 이 남아 있으면 "더 이상
사용되지 않음 — 수동 삭제 안전" 경고가 나옵니다. 스크립트는 사용자 데이터를 삭제하지
않으므로, 원하면 사용자가 직접 지우도록 안내하세요.

`provenance: self-improving-skills` 마커는 리네임과 무관하게 유지되는 값이므로 변경
목록에 나타나지 않는 것이 정상입니다.

## 2. 사용자 확인 후 적용

변경 목록이 있으면 사용자에게 적용 여부를 확인받고:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/migrate_local.py" --apply
```

각 파일은 적용 직전 `<파일>.bak-migration-<타임스탬프>` 로 백업됩니다. 재실행은
멱등(이미 최신이면 no-op)입니다.

## 3. 마켓플레이스 재등록 (스크립트가 안내하는 경우)

스크립트가 `known_marketplaces.json`/`installed_plugins.json` 에 구 마켓플레이스 항목이
남아 있다고 경고하면, 레지스트리는 CLI가 관리하므로 직접 편집하지 말고 재등록하세요:

```bash
claude plugin marketplace remove samton-claude
claude plugin marketplace add samton-inc/samton-plugins
# 이전에 쓰던 플러그인을 새 이름으로 재설치 (예)
claude plugin install claude-code-self-improving-skills@samton-plugins
claude plugin install feature@samton-plugins
```

재등록은 rename된 GitHub 리포에서 fresh clone을 받아오므로, 구버전 클론(누락 플러그인
포함) 문제도 함께 해결됩니다.

## 4. 마무리 안내

- **Claude Code 재시작**을 안내하세요 — settings.json 변경은 재시작 후 반영되며, 실행
  중인 다른 세션이 있으면 종료 시 settings.json 을 덮어쓸 수 있습니다(가능하면 다른
  세션을 먼저 닫고 적용하는 것이 안전).
- codex 사용자라면: 다음 codex 시작 시 훅 재신뢰 프롬프트가 뜰 수 있음(정상 — 승인하면
  됩니다).
- 적용 후 dry-run을 한 번 더 돌려 "이미 최신 상태" 가 나오는지 확인하면 완료입니다.

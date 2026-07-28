# Codex Self Improvement

This plugin implements a Hermes-inspired learning loop for Codex:

- `PostToolUse` records tool telemetry, counts tool iterations toward the
  review trigger, and watches for skill edits that bypassed the skill manager
  (snapshot diff — a direct shell edit gets validated, its patch telemetry
  repaired, and a post-hoc checkpoint backup).
- `Stop` queues a non-blocking background review by default when a trigger
  fires, then exits without replacing the source task's final answer. The interval
  trigger counts accumulated tool iterations (default 10,
  `CODEX_SELF_IMPROVE_INTERVAL`) — not Stop turns — and real skill work
  (create/patch/write through the manager) resets the counter. The signal
  regex matches only explicit corrective phrasings ("next time", "다음부터",
  "틀렸" …), and a transcript signal is consumed once seen so the same old
  message never re-fires the review. `$skill-name` mentions in new transcript
  rows are attributed to that skill's use count. `foreground` compatibility
  mode retains the former same-turn continuation; `off` records signals only.
- `SessionStart` injects a tiny status note, plus an interval-gated curator
  nudge: when the last curate pass is older than
  `CODEX_SELF_IMPROVE_CURATE_INTERVAL_DAYS` (default 7) and at least
  `CODEX_SELF_IMPROVE_CURATE_MIN_SKILLS` (default 8) skills are tracked, a
  dry-run curate runs in-process and a one-line candidate summary is injected.
  First sight only seeds the clock (never curates right after install).
- Skills guide review and curator workflows. The review skill follows the
  Hermes ladder: patch the skill that was in play > extend an existing skill >
  embed the corrected preference > create new (last resort). "Nothing to
  save." is a real option, but not the default.
- A local MCP/CLI skill manager creates backups, tracks usage (viewing a
  skill counts as a use — loading is behavioural intent), pins important
  skills, archives stale skills (timestamp-suffixed on collision), restores
  archived skills, lists/restores/prunes backups (rollback is itself
  undoable), and runs an advisory security scan (secrets / prompt injection /
  invisible unicode / local paths — findings never block a local write).
- New skills are provenance-stamped in frontmatter
  (`metadata.provenance: self-improving-skills`), so agent-created skills
  stay curation-eligible even if the usage sidecar is lost or the skill moves
  machines. Every curate run persists a report under the data dir's
  `logs/curator/<utc>/`.

## Installation

Add the repository's Codex marketplace and install the plugin:

```bash
codex plugin marketplace add UniM0cha/self-improving-skills
codex plugin add chatgpt-codex-self-improving-skills@self-improving-skills
```

For an existing installation, refresh the marketplace and reinstall the plugin:

```bash
codex plugin marketplace upgrade self-improving-skills
codex plugin add chatgpt-codex-self-improving-skills@self-improving-skills
```

After installing or upgrading, CLI users should start a new Codex task. Desktop
users should quit and reopen the app, then start a new task so the new plugin
package and hooks are loaded.

The default Stop hook queues a short background review after an explicit
correction signal or the configured tool-iteration threshold. To disable
automatic review for a shell-launched Codex process:

```bash
export CODEX_SELF_IMPROVE_MODE=off
```

For the desktop app, set the value persistently in `~/.codex/config.toml`
(merge it into an existing table instead of declaring the table twice):

```toml
[shell_environment_policy.set]
CODEX_SELF_IMPROVE_MODE = "off"
```

An `export` made after the desktop app has started does not change that
already-running app's environment. Set `CODEX_SELF_IMPROVE_MODE=foreground`
only when the legacy visible same-turn review is intentionally desired.

## Background review worker

The Stop hook persists a small job record in the plugin data directory and
returns no model-visible output. A detached Python worker then runs `codex exec`
with an ephemeral session, hooks disabled, and a workspace-write sandbox. The
source turn's model is preferred; if that model is unavailable, the worker
retries with the user's configured default model. Successful reviews remain
silent. Runner, authentication, exhausted-retry, and read-only repo-skill
candidate states are reported as one short SessionStart advisory.

The child uses `--ignore-user-config` and `--ignore-rules`, clears ambient MCP
configuration, disables plugins and interactive or shell-capable built-in
tools, and registers only this plugin's skill-manager MCP. It copies only the
user's default model and reasoning effort from `config.toml`; unrelated MCP,
tool, plugin, and execution-policy settings are not inherited. Because
`codex exec` is non-interactive, that one isolated manager is explicitly
auto-approved with an allowlist limited to list, view, create, patch,
support-file write, and scan operations; no other MCP server or built-in
mutation tool is enabled.

Each background job starts a separate Codex run and therefore consumes
additional tokens and account usage. A command failure is retried at most
twice, after 30 seconds and 5 minutes, for three total attempts. Authentication
failures remain blocked without consuming those retries; after signing in, use
`review-retry` to make the job pending again. Each attempt has a 10-minute
execution limit.

The queue stores transcript path and an exact parsed-row cutoff, never a
transcript copy. The worker reads a bounded window ending at that cutoff in
memory and treats it as untrusted evidence. Child hooks and automatic review
are disabled to prevent recursion. The worker never uses danger-full-access or
approval bypass flags.

Background jobs may automatically change only personal skills under
`~/.codex/skills` and `~/.agents/skills`. Repo skills remain readable for
duplicate detection, but proposed changes to them are saved as candidates for
foreground review. Before every automatic write, the existing manager backup,
validation, scan, and read-before-write protections still apply.

Use `CODEX_SELF_IMPROVE_CODEX_BIN` when the `codex` executable is not available
on the hook process's `PATH`. Missing CLI or authentication never falls back to
a foreground review. A missing executable leaves the durable job pending for a
later SessionStart launch; an authentication failure remains blocked until the
user signs in and explicitly retries it. CLI/MCP status and job commands expose
queue state without returning transcript contents. Completed and terminally
failed metadata and result files are retained for 30 days. Pending and blocked
jobs are never removed automatically.

Queue operations are available from the bundled CLI:

```bash
python3 scripts/skill_manager_cli.py review-jobs --limit 20
python3 scripts/skill_manager_cli.py review-retry JOB_ID
python3 scripts/skill_manager_cli.py review-worker --once
```

The MCP server exposes the same operations as `codex_review_jobs`,
`codex_review_retry`, and `codex_review_run_worker`. The main status response
includes `review_mode`, `automatic_review`, per-state queue counts, worker
state, and sanitized last-failure metadata. The legacy `auto_continue` field is
deprecated and is true only in explicit `foreground` mode.

State is stored in `PLUGIN_DATA` when Codex provides it. Installed MCP/CLI
processes derive the same official directory from their cache path:
`~/.codex/plugins/data/<plugin>-<marketplace>`. A source checkout that has
neither value keeps the legacy `~/.self-improving-skills` fallback.

By default, new skills created through the manager are written to
`~/.codex/skills`. The manager can also read existing user skills from
`~/.agents/skills` for compatibility, and `CODEX_SELF_IMPROVE_CREATE_ROOT`
can override the create location.

## Read-before-write guard

Patching or overwriting an EXISTING skill file requires reading it first
(Hermes rule: edit what you actually saw, not what you remember):

- **MCP**: `codex_skill_view` registers the resolved file path; `codex_skill_patch`
  and `codex_skill_write_file` reject unviewed existing targets. Creating a
  new file is exempt. The guard applies to the whole MCP session. Background
  workers additionally set a personal-only write-root allowlist; foreground
  sessions keep the existing visible-root behavior unless explicitly limited.
- **CLI**: the process dies between calls, so the CLI approximates with the
  skill's `last_viewed_at` (within 30 minutes, skill-level). Humans can pass
  `--force-unviewed`.

## Environment variables

| variable | default | meaning |
|---|---|---|
| `CODEX_SELF_IMPROVE_MODE` | `background` | `background` queues a detached review, `foreground` uses the legacy Stop continuation, and `off` records signals without running a review; invalid values fail closed to `off` |
| `CODEX_SELF_IMPROVE_AUTO` | (compatibility alias) | used only when MODE is unset; truthy maps to `background`, any explicit non-truthy value maps to `off` |
| `CODEX_SELF_IMPROVE_CODEX_BIN` | PATH lookup | explicit `codex` executable used by the background worker |
| `CODEX_SELF_IMPROVE_INTERVAL` | `10` | tool iterations since the last review/skill-work that trigger the interval review (0 disables) |
| `CODEX_SELF_IMPROVE_CURATE_INTERVAL_DAYS` | `7` | days between SessionStart curator nudges |
| `CODEX_SELF_IMPROVE_CURATE_MIN_SKILLS` | `8` | tracked-skill count below which the curator nudge stays silent |
| `CODEX_SELF_IMPROVE_SKILL_ROOTS` | (auto) | override the skill root search list |
| `CODEX_SELF_IMPROVE_WRITE_ROOTS` | all visible roots | optional mutation allowlist; the background worker sets personal roots only |
| `CODEX_SELF_IMPROVE_CREATE_ROOT` | `~/.codex/skills` | where new skills are created |

## Data migration

The CLI can import older stores without deleting them. The command is a
read-only dry-run unless `--apply` is present. When running the CLI from a
source checkout, pass the installed marketplace's canonical data directory
explicitly:

```bash
python3 scripts/skill_manager_cli.py migrate-data --source ~/.self-improving-skills --target ~/.codex/plugins/data/chatgpt-codex-self-improving-skills-self-improving-skills
python3 scripts/skill_manager_cli.py migrate-data --source ~/.self-improving-skills --target ~/.codex/plugins/data/chatgpt-codex-self-improving-skills-self-improving-skills --apply
```

These examples assume the default Codex home (`~/.codex`). If Codex uses a
custom home, replace that prefix with the active Codex home path.

An installed-cache copy derives that same target automatically, so
`--target` can be omitted there. Stop other Codex processes that still use the
legacy source before applying. The migration verifies that the source stayed
stable while it was snapshotted and aborts before importing active data if it
detects a concurrent change.

Before applying, it snapshots both source and target under a sibling
`<target>-migration-backups/` directory. Skill usage metadata is merged
conservatively, live target tool/session counters stay authoritative, backup
content is deduplicated, and historical logs/signals/state are preserved under
`imports/` instead of being replayed into live state. Re-running the same
import is idempotent.

## MCP server startup contract

`.mcp.json` starts the skill manager with `cwd: "."` (the plugin root), a
relative script path, and an allowlist of documented `CODEX_SELF_IMPROVE_*`
settings. The server resolves the plugin root from `PLUGIN_ROOT` when Codex
provides it, else from its own `__file__` location, and derives Codex's
writable plugin-data directory from an installed cache path when
`PLUGIN_DATA` is absent. `serverInfo.version` is read from
`.codex-plugin/plugin.json` at initialize time (a hardcoded version literal
drifted from the manifest once; never reintroduce one).

## Recommended reasoning effort

Foreground review and curator passes inherit the Codex session's
`model_reasoning_effort`. Background review receives the source model from the
Stop payload but uses the user's configured default reasoning effort because the
current Stop payload does not expose the turn's effort (`~/.codex/config.toml`;
codex CLI 0.144+ accepts `minimal`–`xhigh`, `max`, `ultra`):

- Day-to-day post-turn reviews: the default (`medium`) is enough.
- Large consolidation passes (`$codex-skill-curator` over many skills):
  `high`–`xhigh` is worth it.
- `ultra` is not just "more reasoning": it enables proactive multi-agent
  behaviour and can hit transient "Selected model is at capacity" errors —
  one retry usually clears them. Don't leave it on for routine passes.

Per-skill effort pinning in `agents/openai.yaml` is NOT supported by the
current schema (interface/dependencies/policy only) — don't add model fields
there.

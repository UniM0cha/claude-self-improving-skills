"""Stop-hook contract tests for analyze_turn.py (run as a real subprocess)."""

import json

from conftest import tool_use


def _work_rows(calls=12, edits=2):
    rows = [tool_use("Bash", {"command": "x"}) for _ in range(calls)]
    rows += [tool_use("Edit", {"file_path": "/tmp/f{0}.py".format(i)}) for i in range(edits)]
    return rows


def test_nudge_fires_at_threshold(run_analyzer):
    r = run_analyzer(_work_rows(), "s")
    assert r["decision"] == "block"
    assert "transcript" in r["reason"] and "s.jsonl" in r["reason"]


# --- background mode --------------------------------------------------------

BACKGROUND = {"SIS_REVIEW_MODE": "background", "SIS_TEST_NO_LAUNCH": "1"}


def _usable_claude(tmp_path):
    """A `claude` that passes the hook's preflight (present, current, signed in).

    Without one the hook correctly refuses to queue, so a test of the queueing
    path would silently be testing the fallback instead.

    Named `.py` and launched through sys.executable by the worker, so it needs
    no executable bit.
    """
    fake = tmp_path / "claude.py"
    fake.write_text(
        "import sys\n"
        "args = sys.argv[1:]\n"
        "if '--version' in args:\n"
        "    print('2.1.217 (Claude Code)')\n"
        "    raise SystemExit(0)\n"
        "print('{\"loggedIn\": true}')\n",
        encoding="utf-8")
    return dict(BACKGROUND, SIS_CLAUDE_BIN=str(fake))


def _queue(sandbox):
    import distill_queue
    return distill_queue.DistillQueue(
        sandbox.home / ".claude" / "self-improve" / "distill-jobs.sqlite3")


def test_background_mode_queues_the_work_and_stays_silent(run_analyzer, sandbox, tmp_path):
    r = run_analyzer(_work_rows(), "s", extra={"prompt_id": "p1", "cwd": "/work"},
                     env=_usable_claude(tmp_path))
    # The whole point: the user's turn ends exactly as it would without the
    # plugin installed — no block, no visible instruction.
    assert r["decision"] == "approve"
    jobs = _queue(sandbox).list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["prompt_id"] == "p1"
    assert jobs[0]["cwd"] == "/work"


def test_background_mode_captures_the_final_message_from_the_payload(
        run_analyzer, sandbox, tmp_path):
    run_analyzer(_work_rows(), "s",
                 extra={"prompt_id": "p1", "last_assistant_message": "the conclusion"},
                 env=_usable_claude(tmp_path))
    # The transcript is written asynchronously and may not hold it yet.
    assert _queue(sandbox).list_jobs()[0]["last_assistant_message"] == "the conclusion"


def test_background_mode_falls_back_to_the_nudge_without_a_cli(run_analyzer, sandbox):
    env = dict(BACKGROUND, SIS_CLAUDE_BIN="/nonexistent/claude")
    r = run_analyzer(_work_rows(), "s", extra={"prompt_id": "p1"}, env=env)
    # A missing CLI must not mean no distillation at all.
    assert r["decision"] == "block"
    assert _queue(sandbox).list_jobs() == []


def test_background_mode_falls_back_when_the_cli_cannot_actually_run(
        run_analyzer, sandbox, tmp_path):
    """Queueing on a machine where the CLI is unauthenticated would end the
    turn silently and surface the failure only in a blocked job nobody reads."""
    fake = tmp_path / "claude.py"  # launched via sys.executable; no exec bit needed
    fake.write_text(
        "import sys\n"
        "args = sys.argv[1:]\n"
        "if '--version' in args:\n"
        "    print('2.1.217 (Claude Code)')\n"
        "    raise SystemExit(0)\n"
        "print('{\"loggedIn\": false}')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8")
    env = dict(BACKGROUND, SIS_CLAUDE_BIN=str(fake))
    r = run_analyzer(_work_rows(), "s", extra={"prompt_id": "p1"}, env=env)
    assert r["decision"] == "block"
    assert _queue(sandbox).list_jobs() == []


def test_consecutive_segments_each_get_their_own_job(run_analyzer, sandbox, tmp_path):
    """A daily spawn cap used to stop the second one here. It counted attempts
    rather than successes, so a day of failures silently refused healthy work;
    removed in v0.17.0. Per-job budget and the triggers are the ceilings now."""
    env = _usable_claude(tmp_path)
    run_analyzer(_work_rows(), "a", extra={"prompt_id": "p1"}, env=env)
    run_analyzer(_work_rows(), "b", extra={"prompt_id": "p2"}, env=env)
    assert len(_queue(sandbox).list_jobs()) == 2


def test_off_mode_neither_queues_nor_nudges(run_analyzer, sandbox):
    r = run_analyzer(_work_rows(), "s", env={"SIS_REVIEW_MODE": "off"})
    assert r["decision"] == "approve"
    assert _queue(sandbox).list_jobs() == []


def test_a_background_session_s_own_stop_hook_stands_down(run_analyzer, sandbox, tmp_path):
    """Without this the child would queue another job, which would spawn
    another child, forever."""
    r = run_analyzer(_work_rows(), "s", extra={"prompt_id": "p1"},
                     env=dict(_usable_claude(tmp_path), SIS_BACKGROUND_JOB="1"))
    assert r["decision"] == "approve"
    assert _queue(sandbox).list_jobs() == []


def test_a_one_line_core_edit_alone_does_not_spawn_a_session(run_analyzer, sandbox, tmp_path):
    rows = [tool_use("Edit", {"file_path": "/x/claude-code-self-improving-skills/scripts/a.py"})]
    r = run_analyzer(rows, "s", extra={"prompt_id": "p1"}, env=_usable_claude(tmp_path))
    # core_touched has no threshold of its own; as a background trigger it
    # would otherwise fire on every turn spent working in this repository.
    assert r["decision"] == "approve"
    assert _queue(sandbox).list_jobs() == []


def test_below_call_threshold_approves(run_analyzer):
    assert run_analyzer(_work_rows(calls=9), "s")["decision"] == "approve"


def test_below_edit_threshold_approves(run_analyzer):
    assert run_analyzer(_work_rows(edits=1), "s")["decision"] == "approve"


def test_nudge_fires_only_once_per_segment(run_analyzer):
    rows = _work_rows()
    assert run_analyzer(rows, "s")["decision"] == "block"
    # Same accumulated work, next Stop: the recorded nudge suppresses a repeat.
    assert run_analyzer(rows, "s")["decision"] == "approve"


def test_renudges_after_another_threshold_of_work(run_analyzer):
    rows = _work_rows()
    assert run_analyzer(rows, "s")["decision"] == "block"
    rows2 = rows + _work_rows()
    assert run_analyzer(rows2, "s")["decision"] == "block"


def test_distillation_anchor_resets_counting(run_analyzer, sandbox):
    rows = _work_rows()
    rows.append(tool_use("Task", {"subagent_type": "claude-code-self-improving-skills:skill-distiller",
                                  "prompt": "distill"}))
    assert run_analyzer(rows, "s")["decision"] == "approve"


def test_stop_hook_active_loop_guard(run_analyzer):
    r = run_analyzer(_work_rows(), "s", {"stop_hook_active": True})
    assert r["decision"] == "approve"


def test_empty_payload_fails_safe(sandbox):
    from conftest import _run_script
    out = _run_script(sandbox.home, "analyze_turn.py", {})
    assert json.loads(out)["decision"] == "approve"


def test_core_advisory_fires_once(run_analyzer):
    core = [tool_use("Edit", {"file_path": "/r/plugins/claude-code-self-improving-skills/x.py"})]
    r = run_analyzer(core, "s")
    assert r["decision"] == "block" and "코어 소스" in r["reason"]
    assert run_analyzer(core, "s")["decision"] == "approve"


def test_readonly_segment_nudges_at_higher_threshold(run_analyzer):
    rows = [tool_use("Bash", {"command": "x"}) for _ in range(24)]
    r = run_analyzer(rows, "ro")
    assert r["decision"] == "block"
    assert "조사·디버깅" in r["reason"]  # read-only guidance branch


def test_readonly_below_threshold_approves(run_analyzer):
    rows = [tool_use("Bash", {"command": "x"}) for _ in range(23)]
    assert run_analyzer(rows, "ro2")["decision"] == "approve"


def test_distiller_model_env_included_in_nudge(run_analyzer, monkeypatch):
    monkeypatch.setenv("SIS_DISTILLER_MODEL", "sonnet")
    r = run_analyzer(_work_rows(), "m1")
    assert r["decision"] == "block" and 'model="sonnet"' in r["reason"]


def test_distiller_model_haiku_ignored(run_analyzer, monkeypatch):
    monkeypatch.setenv("SIS_DISTILLER_MODEL", "haiku")  # 정책: 서브에이전트 Haiku 금지
    r = run_analyzer(_work_rows(), "m2")
    assert r["decision"] == "block" and "model=" not in r["reason"]


def test_seed_labels_user_vs_distilled(run_analyzer, sandbox, store_data):
    sandbox.make_skill("hand-made")
    sandbox.make_skill("auto-made",
                       "---\nname: auto-made\ndescription: d\nmetadata:\n"
                       "  provenance: self-improving-skills\n  origin: distilled\n---\nbody\n")
    rows = [tool_use("Read", {"file_path": str(sandbox.skills / "hand-made" / "SKILL.md")}),
            tool_use("Read", {"file_path": str(sandbox.skills / "auto-made" / "SKILL.md")})]
    run_analyzer(rows, "s")
    data = store_data()
    assert data["hand-made"]["created_by"] == "user"
    assert data["auto-made"]["created_by"] == "agent"


def test_maintenance_segment_views_not_counted(run_analyzer, sandbox, store_data):
    sandbox.make_skill("some-skill")
    sp = str(sandbox.skills / "some-skill" / "SKILL.md")
    run_analyzer([tool_use("Skill", {"skill": "claude-code-self-improving-skills:curate-skills"}),
                  tool_use("Read", {"file_path": sp})], "maint")
    data = store_data()
    assert data.get("some-skill", {}).get("view_count", 0) == 0
    # a normal segment still counts the view
    run_analyzer([tool_use("Read", {"file_path": sp})], "normal")
    assert store_data()["some-skill"]["view_count"] == 1


def test_patch_not_counted_from_transcript_scan(run_analyzer, sandbox, store_data):
    sandbox.make_skill("some-skill")
    sp = str(sandbox.skills / "some-skill" / "SKILL.md")
    run_analyzer([tool_use("Edit", {"file_path": sp})], "s")
    assert store_data().get("some-skill", {}).get("patch_count", 0) == 0


def test_skill_use_counted(run_analyzer, sandbox, store_data):
    sandbox.make_skill("some-skill")
    run_analyzer([tool_use("Skill", {"skill": "some-skill"})], "s")
    assert store_data()["some-skill"]["use_count"] == 1

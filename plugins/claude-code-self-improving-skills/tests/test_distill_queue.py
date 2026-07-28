"""Contract tests for the background-distillation job queue.

The queue is the piece that makes background distillation safe to run from a
Stop hook: it decides what gets retried, what never gets retried, and — most
importantly — that two workers never distil the same session at once.
"""

import os

import pytest

import distill_queue
from distill_queue import MAX_ATTEMPTS, RETRY_DELAYS_SECONDS, DistillQueue


@pytest.fixture
def queue(tmp_path):
    return DistillQueue(tmp_path / "jobs.sqlite3")


def _enqueue(q, *, session="s1", prompt="p1", rows=10, signal=False,
             source="none", trigger="interval", **kw):
    return q.enqueue(
        session_id=session, prompt_id=prompt, transcript_path="/tmp/t.jsonl",
        transcript_rows=rows, signal=signal, signal_source=source,
        trigger=trigger, **kw)


# --- enqueue: dedup and coalescing -----------------------------------------

def test_the_same_prompt_twice_does_not_create_a_second_job(queue):
    first = _enqueue(queue)
    second = _enqueue(queue)
    assert second["duplicate"] is True
    assert second["job_id"] == first["job_id"]
    assert len(queue.list_jobs()) == 1


def test_a_new_prompt_coalesces_into_the_session_s_pending_job(queue):
    first = _enqueue(queue, prompt="p1", trigger="interval")
    second = _enqueue(queue, prompt="p2", trigger="signal", signal=True,
                      source="last_user_message")
    assert second["coalesced"] is True
    assert second["job_id"] == first["job_id"]
    job = queue.get(first["job_id"])
    # The merged job must carry BOTH reasons: dropping the signal would lose
    # the user correction that made this turn worth distilling.
    assert job["signal"] is True
    assert job["trigger"] == "signal+interval"
    assert job["prompt_id"] == "p2"


def test_signal_source_accumulates_across_coalesced_prompts(queue):
    _enqueue(queue, prompt="p1", signal=True, source="last_user_message")
    job_id = _enqueue(queue, prompt="p2", signal=True,
                      source="transcript_user_messages")["job_id"]
    assert queue.get(job_id)["signal_source"] == (
        "last_user_message+transcript_user_messages")


def test_different_sessions_get_their_own_jobs(queue):
    a = _enqueue(queue, session="s1", prompt="p1")
    b = _enqueue(queue, session="s2", prompt="p1")
    assert a["job_id"] != b["job_id"]


def test_a_running_job_does_not_absorb_a_new_prompt(queue):
    first = _enqueue(queue, prompt="p1")
    queue.claim_next("w1")
    second = _enqueue(queue, prompt="p2")
    # Coalescing into a running job would silently change the evidence window
    # under the worker's feet.
    assert second["coalesced"] is False
    assert second["job_id"] != first["job_id"]


def test_last_assistant_message_is_stored_and_bounded(queue):
    job_id = _enqueue(queue, last_assistant_message="x" * 30_000)["job_id"]
    assert len(queue.get(job_id)["last_assistant_message"]) == 20_000


def test_coalescing_keeps_the_previous_final_message_when_none_is_supplied(queue):
    _enqueue(queue, prompt="p1", last_assistant_message="original")
    job_id = _enqueue(queue, prompt="p2")["job_id"]
    assert queue.get(job_id)["last_assistant_message"] == "original"


# --- claim / lease ----------------------------------------------------------

def test_claim_moves_pending_to_running_and_counts_the_attempt(queue):
    _enqueue(queue)
    job = queue.claim_next("w1")
    assert job["status"] == "running"
    assert job["attempts"] == 1
    assert queue.claim_next("w2") is None


def test_worker_lease_is_a_singleton(queue):
    # A real, live pid: the lease steals only when the holder looks dead, and
    # pid 1 is not a queryable live process on Windows (it is on POSIX), which
    # would let the "steal" branch fire and defeat the singleton assertion.
    live = os.getpid()
    assert queue.acquire_worker_lease("w1", pid=live) is True
    assert queue.acquire_worker_lease("w2", pid=live) is False
    assert queue.acquire_worker_lease("w1", pid=live) is True  # re-entrant


def test_worker_lease_is_stealable_once_the_holder_is_gone(queue, monkeypatch):
    queue.acquire_worker_lease("w1", pid=1)
    monkeypatch.setattr(distill_queue, "_pid_alive", lambda pid: False)
    assert queue.acquire_worker_lease("w2", pid=2) is True


def test_a_reused_pid_does_not_count_as_the_original_worker(queue, monkeypatch):
    monkeypatch.setattr(distill_queue, "_pid_identity", lambda pid: "boot-1")
    queue.acquire_worker_lease("w1", pid=4242)
    # PID 4242 is alive again, but it is a different process now.
    monkeypatch.setattr(distill_queue, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(distill_queue, "_pid_identity", lambda pid: "boot-2")
    assert queue.acquire_worker_lease("w2", pid=4242) is True


def test_worker_alive_is_false_when_the_pid_identity_changed(queue, monkeypatch):
    monkeypatch.setattr(distill_queue, "_pid_identity", lambda pid: "boot-1")
    queue.acquire_worker_lease("w1", pid=4242)
    monkeypatch.setattr(distill_queue, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(distill_queue, "_pid_identity", lambda pid: "boot-2")
    assert queue.worker_alive() is False


def test_only_the_lease_holder_can_complete_a_job(queue):
    _enqueue(queue)
    job_id = queue.claim_next("w1")["id"]
    result = {"status": "nothing_to_save", "skills": [], "candidates": [],
              "summary": "-"}
    assert queue.complete(job_id, "impostor", result) is False
    assert queue.complete(job_id, "w1", result) is True


# --- recovery ---------------------------------------------------------------

def test_a_live_worker_is_never_recovered_out_from_under(queue, monkeypatch):
    _enqueue(queue)
    queue.claim_next("w1")
    monkeypatch.setattr(distill_queue, "_pid_alive", lambda pid: True)
    assert queue.recover_expired_jobs() == 0
    assert queue.get(1)["status"] == "running"


def test_a_dead_worker_s_job_returns_to_pending(queue, monkeypatch):
    _enqueue(queue)
    queue.claim_next("w1")
    monkeypatch.setattr(distill_queue, "_pid_alive", lambda pid: False)
    assert queue.recover_expired_jobs() == 1
    job = queue.get(1)
    assert job["status"] == "pending"
    assert job["error_code"] == "worker_interrupted"


def test_a_crashed_worker_s_job_always_reruns_the_whole_pipeline(queue, monkeypatch):
    """Recovery must never complete a job from a leftover model-authored
    result: shape validation does not prove skill_guard ever checked what
    actually landed on disk."""
    _enqueue(queue)
    job_id = queue.claim_next("w1")["id"]
    monkeypatch.setattr(distill_queue, "_pid_alive", lambda pid: False)
    queue.recover_expired_jobs()
    assert queue.get(job_id)["status"] == "pending"


# --- retry / backoff / terminal states --------------------------------------

def test_failures_back_off_then_give_up_after_max_attempts(queue):
    _enqueue(queue)
    delays = []
    for _ in range(MAX_ATTEMPTS):
        job = queue.claim_next("w1")
        assert job is not None
        outcome = queue.fail(job["id"], "w1", code="boom", message="boom")
        delays.append(outcome["retry_delay_seconds"])
        if outcome["status"] == "pending":
            # Make the retry immediately claimable instead of sleeping.
            with queue._connect() as conn:
                conn.execute("UPDATE distill_jobs SET available_at = 0")
    assert delays[:2] == list(RETRY_DELAYS_SECONDS)
    assert queue.get(1)["status"] == "failed"


def test_blocked_jobs_do_not_burn_attempts_and_can_be_retried(queue):
    _enqueue(queue)
    job_id = queue.claim_next("w1")["id"]
    queue.block(job_id, "w1", code="authentication_required", message="sign in")
    job = queue.get(job_id)
    assert job["status"] == "blocked"
    assert job["attempts"] == 1
    assert queue.retry(job_id) is True
    assert queue.get(job_id)["attempts"] == 0


# --- retention --------------------------------------------------------------

def test_cleanup_sweeps_settled_history_but_never_pending_or_blocked(queue):
    _enqueue(queue, session="done-session", prompt="p1")
    done_id = queue.claim_next("w1")["id"]
    queue.complete(done_id, "w1", {"status": "nothing_to_save", "skills": [],
                                   "candidates": [], "summary": "-"})
    _enqueue(queue, session="blocked-session", prompt="p1")
    blocked_id = queue.claim_next("w1")["id"]
    queue.block(blocked_id, "w1", code="authentication_required", message="x")
    _enqueue(queue, session="pending-session", prompt="p1")

    with queue._connect() as conn:
        conn.execute("UPDATE distill_jobs SET completed_at = 0, created_at = 0")

    assert queue.cleanup(retention_days=30) == 1
    remaining = {job["status"] for job in queue.list_jobs()}
    # A blocked job is the record of something a human still has to fix.
    assert remaining == {"blocked", "pending"}


# --- result validation ------------------------------------------------------

def test_validate_result_rejects_an_unknown_status():
    with pytest.raises(ValueError):
        distill_queue.validate_result({"status": "whatever", "skills": [],
                                       "candidates": [], "summary": ""})


def test_validate_result_drops_unknown_keys_instead_of_failing():
    # A chattier model must not fail an otherwise usable job.
    out = distill_queue.validate_result({
        "status": "nothing_to_save", "skills": [], "candidates": [],
        "summary": "-", "chatty_extra": {"nope": 1}})
    assert set(out) == {"status", "skills", "candidates", "summary"}


def test_validate_result_round_trips_the_guard_s_own_fields():
    out = distill_queue.validate_result({
        "status": "changed",
        "skills": [{"name": "foo", "action": "created", "path": "/x/SKILL.md"}],
        "candidates": [], "summary": "-",
        "out_of_scope_writes": ["/home/me/.zshrc"],
        "rolled_back": ["bar"]})
    assert out["out_of_scope_writes"] == ["/home/me/.zshrc"]
    assert out["rolled_back"] == ["bar"]


def test_validate_result_does_not_carry_a_per_job_symlinked_list():
    """Which links exist is answered off the live tree, not from job history.

    A per-run copy was tried and removed: nothing read it back, and it went
    stale the moment a link was added or deleted. `distill_cli status` scans
    the tree at call time instead. Asserted so the plumbing is not quietly
    reintroduced one hop at a time.
    """
    out = distill_queue.validate_result({
        "status": "changed",
        "skills": [{"name": "foo", "action": "created", "path": "/x/SKILL.md"}],
        "candidates": [], "summary": "-",
        "symlinked": ["/home/me/.claude/skills/linked"]})
    assert "symlinked" not in out


def test_validate_result_rejects_a_non_string_out_of_scope_entry():
    with pytest.raises(ValueError):
        distill_queue.validate_result({
            "status": "changed", "skills": [], "candidates": [], "summary": "-",
            "out_of_scope_writes": [{"path": "/etc/passwd"}]})


# --- path safety ------------------------------------------------------------

def test_a_symlinked_queue_path_is_refused(tmp_path):
    real = tmp_path / "real.sqlite3"
    real.write_bytes(b"")
    link = tmp_path / "link.sqlite3"
    link.symlink_to(real)
    with pytest.raises(ValueError):
        DistillQueue(link)

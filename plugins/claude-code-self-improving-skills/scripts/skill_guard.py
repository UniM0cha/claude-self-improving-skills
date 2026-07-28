#!/usr/bin/env python3
"""Post-run safety gate for the background distiller.

The background worker launches `claude -p` with `--permission-mode
bypassPermissions`, because `~/.claude` is a protected path and no other mode
can write there unattended. That mode turns off every built-in check, and the
distiller's input — a session transcript — is untrusted.

This module is the only enforcement left in the worker; the permission fencing
that used to stand in front of it is gone. The README's security section is the
canonical account of that change and what it costs.

What this module is, precisely: detection and rollback, not prevention. A bad
write happens first and is undone after, and only inside the skill tree. That
tree is fully snapshotted, so a write there is always caught. Outside it,
nothing stops a write and it is detected only if it lands on a bounded
watchlist of high-value files — a full-filesystem snapshot is not feasible.

Symlinked entries are the one gap inside the tree. Following a link would pull
arbitrary files into the snapshot or loop, so the walk stops there and a write
through one is neither reverted nor flagged. `symlinked_entries()` lists them
off the live tree for callers who need to know. A link is therefore a
write-bridge out of the tree with nothing behind it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from typing import Any, Dict, List, Optional, Set

import skill_paths
import validate_skill

try:
    import usage_store
except Exception:  # pragma: no cover - telemetry is best-effort
    usage_store = None

# A skill is capped at 100_000 chars by the validator; this bounds the whole
# snapshot so a pathological library can't exhaust the worker's memory. Past
# the cap we keep hashes (detection still works) but lose rollback content,
# which is reported rather than silently accepted.
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


def watchlist(home: Optional[str] = None) -> List[str]:
    """High-value files outside the skill tree that must never change.

    Kept deliberately short: every entry is a file whose modification would
    grant persistence or exfiltration, so a hit here is worth interrupting the
    user for. This is detection only — nothing prevents these writes. The list
    used to mirror the worker's deny rules; those are empty now, so a hit means
    the write already happened.

    NOT here: `.claude.json`. The child IS a Claude Code session, and the CLI
    rewrites that global-state file as normal operation — trust prompts, recent
    projects, MCP state — through its own internals rather than a tool call.
    Watching it would flag every healthy distillation as an out-of-scope write
    (confirmed against a real `claude -p` run). It was covered by the deny rules
    instead; with those gone it is neither denied nor watched, so a tool-call
    write to it now passes unnoticed.
    """
    base = home or skill_paths.user_home()
    relative = (
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".claude/CLAUDE.md",
        ".zshrc",
        ".zprofile",
        ".zshenv",
        ".bashrc",
        ".bash_profile",
        ".profile",
        ".envrc",
        ".npmrc",
        ".gitconfig",
    )
    # Split each relative entry on "/" so os.path.join yields native separators
    # — otherwise a Windows path is "C:\\home\\.claude/settings.json", a mixed
    # form that no normalized path (or exact-string report check) will match.
    return [os.path.join(base, *name.split("/")) for name in relative]


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest_stream(path: str) -> Optional[str]:
    """Hash a file too large to hold in memory, so it is still change-detected."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _read(path: str, *, follow: bool = False) -> Optional[bytes]:
    try:
        if not follow and os.path.islink(path):
            return None
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _walk_skill_tree(root: str):
    """Yield (files, symlinks) found under `root`.

    EVERY file is snapshotted, not just SKILL.md. A skill directory legitimately
    carries `references/*.md` and `scripts/*.py`, and a script inside a skill is
    executable content the user runs later — so leaving those out of the
    snapshot would mean the guard could neither detect nor revert the single
    most dangerous thing an untrusted distiller could write.

    Symlinks are never followed: a link out of the tree would pull arbitrary
    files into the snapshot, and a link back into it would loop. Claude Code's
    own skill discovery does follow them, so a symlinked entry is real to the
    user while being invisible here — hence the second return value, which
    `symlinked_entries()` uses to answer which paths the guard cannot cover.
    `verify()` does not report them at all; see the module docstring.
    """
    files: List[str] = []
    symlinks: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept = []
        for name in dirnames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                symlinks.append(full)
            else:
                # Dot directories are walked too. `.archive` holds skills the
                # curator can restore later, so a change there is a change to a
                # skill the user will eventually run — excluding it would leave
                # the one place an unattended run could edit unobserved.
                kept.append(name)
        dirnames[:] = kept
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            if os.path.islink(full):
                symlinks.append(full)
            else:
                files.append(full)
    return files, symlinks


def symlinked_entries(root: Optional[str] = None) -> List[str]:
    """Links under the skill tree — exactly the paths the guard cannot cover.

    Links are neither refused nor reverted (see the module docstring), and
    `verify()` does not mention them, so this is the only way to find out which
    paths sit outside the guard's reach. Answered off the tree as it stands, so
    it stays true as links are added and removed.
    """
    target = root or skill_paths.personal_skills_root()
    if not os.path.isdir(target):
        return []
    _files, symlinks = _walk_skill_tree(target)
    return sorted(symlinks)


def _owning_skill(path: str, root: str) -> Optional[str]:
    """The skill directory that `path` belongs to, or None if it sits loose.

    A file directly under the skills root belongs to no skill — nothing should
    ever write there, so it is treated as a violation rather than an asset.
    """
    try:
        relative = os.path.relpath(path, root)
    except ValueError:
        return None
    parts = relative.replace("\\", "/").split("/")
    if len(parts) < 2 or parts[0] in ("", ".", ".."):
        return None
    return os.path.join(root, parts[0])


class Snapshot:
    """The state of the skill tree and the watchlist at one moment.

    File contents are written to `store`, not held in memory. If the worker is
    killed between the child's writes and `verify`, an in-memory baseline would
    die with it and the queue's retry would then run against the already-mutated
    tree — permanently losing the original and accepting whatever the first run
    left behind. On disk, the baseline survives to be restored.
    """

    def __init__(self, root: str, home: Optional[str] = None, store: Optional[str] = None) -> None:
        self.root = root
        self.home = home
        self.store = store
        self.files: Dict[str, str] = {}
        self.modes: Dict[str, int] = {}
        self.watched: Dict[str, Optional[str]] = {}
        self.patch_counts: Dict[str, int] = {}
        self.unbacked: Set[str] = set()

    def capture(self) -> "Snapshot":
        total = 0
        if os.path.isdir(self.root):
            # Links are not snapshotted (see the module docstring) and no longer
            # reported per run either — `symlinked_entries()` answers that off
            # the live tree, which stays true between runs.
            paths, _symlinks = _walk_skill_tree(self.root)
            for path in paths:
                try:
                    info = os.stat(path)
                except OSError:
                    # Enumerated but unreadable. Recording it as unbacked keeps
                    # verify() honest: if it is replaced later we must not treat
                    # the replacement as a brand-new file we can simply accept.
                    self.files[path] = "unreadable"
                    self.unbacked.add(path)
                    continue
                self.modes[path] = stat.S_IMODE(info.st_mode)
                # Check the size BEFORE reading: one multi-gigabyte reference
                # file would otherwise exhaust the worker's memory on the way
                # to discovering it is over the cap.
                if info.st_size > MAX_SNAPSHOT_BYTES or total + info.st_size > MAX_SNAPSHOT_BYTES:
                    digest = _digest_stream(path)
                    self.files[path] = digest if digest is not None else "unreadable"
                    self.unbacked.add(path)
                    continue
                data = _read(path)
                if data is None:
                    self.files[path] = "unreadable"
                    self.unbacked.add(path)
                    continue
                self.files[path] = _digest(data)
                if self._save(path, data):
                    total += len(data)
                else:
                    self.unbacked.add(path)
        for path in watchlist(self.home):
            # Followed on purpose: a dotfiles setup where ~/.zshrc is a symlink
            # is normal, and hashing the link itself would report "absent" for
            # a file the child can very much write through.
            data = _read(path, follow=True)
            self.watched[path] = _digest(data) if data is not None else None
        self.patch_counts = _patch_counts()
        return self

    def _slot(self, path: str) -> Optional[str]:
        if not self.store:
            return None
        digest = hashlib.sha256(path.encode("utf-8", "surrogateescape")).hexdigest()
        return os.path.join(self.store, digest)

    def _save(self, path: str, data: bytes) -> bool:
        slot = self._slot(path)
        if slot is None or self.store is None:
            return False
        try:
            os.makedirs(self.store, exist_ok=True)
            with open(slot, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            return True
        except OSError:
            return False

    def original(self, path: str) -> Optional[bytes]:
        """The pre-run bytes of `path`, or None if no baseline was kept."""
        slot = self._slot(path)
        return _read(slot) if slot else None

    def discard(self) -> None:
        if self.store and os.path.isdir(self.store):
            shutil.rmtree(self.store, ignore_errors=True)


def _patch_counts() -> Dict[str, int]:
    """Per-skill patch_count, used to avoid double-counting a write that the
    child's own PostToolUse hook already recorded."""
    if usage_store is None:
        return {}
    try:
        records = usage_store.all_records()
    except Exception:
        return {}
    counts: Dict[str, int] = {}
    for name, record in records.items():
        if isinstance(record, dict):
            try:
                counts[name] = int(record.get("patch_count", 0))
            except (TypeError, ValueError):
                counts[name] = 0
    return counts


def _restore(path: str, data: Optional[bytes], mode: Optional[int] = None) -> bool:
    """Put `path` back the way the snapshot found it (deleting it if it was new).

    The mode is restored as well: reverting an executable `scripts/run.sh` under
    the process umask would turn 0755 into 0644 and leave a "successfully rolled
    back" skill that no longer runs.
    """
    try:
        if data is None:
            if os.path.isfile(path) and not os.path.islink(path):
                os.unlink(path)
            return True
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        temporary = os.path.join(directory, ".skill-guard-{0}.tmp".format(os.getpid()))
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
        return True
    except OSError:
        return False


def _is_pinned(name: str, previous_text: Optional[str]) -> bool:
    """Pinned per the usage record, or per the PRE-run text (the run itself
    could have stripped the marker)."""
    if usage_store is not None:
        try:
            if bool(usage_store.all_records().get(name, {}).get("pinned")):
                return True
        except Exception:
            pass
    if previous_text is not None:
        return validate_skill._frontmatter_has_pin(previous_text)
    return False


def _has_valid_skill(owner: str) -> bool:
    """Whether the directory currently holds a SKILL.md that passes validation."""
    text = _decode(_read(os.path.join(owner, "SKILL.md")))
    return text is not None and not validate_skill._validate(text)


def _skill_dir_is_pinned(owner: str, before: "Snapshot") -> bool:
    """Whether the skill owning a directory is pinned.

    Needed for asset writes: a run can drop a script into a pinned skill
    without touching its SKILL.md, and the pin marker lives in that file's
    frontmatter — not in the usage record, which a never-used skill has no row
    in. Read the pre-run baseline where one exists, since the run could have
    rewritten the marker away.
    """
    skill_md = os.path.join(owner, "SKILL.md")
    text = _decode(before.original(skill_md)) or _decode(_read(skill_md))
    return _is_pinned(skill_paths.skill_name(skill_md), text)


def _decode(data: Optional[bytes]) -> Optional[str]:
    if data is None:
        return None
    try:
        return data.decode("utf-8", "replace")
    except Exception:
        return None


def verify(before: Snapshot) -> Dict[str, Any]:
    """Re-check the skill tree after the child ran; revert anything unsafe.

    Returns a report the worker merges into the job result:
      installed            skills whose new SKILL.md passed validation
      assets               accepted non-SKILL.md files (references/, scripts/)
      rolled_back          files reverted (invalid, pinned, loose, or escaped)
      out_of_scope_writes  watchlist files that changed
      unprotected          paths the guard could not have reverted

    Symlinked entries appear in none of these: they are neither snapshotted nor
    reverted nor flagged (see the module docstring). `symlinked_entries()` lists
    them off the live tree for anyone who needs to know.

    A skill is judged as a unit: if its SKILL.md is rejected, its assets go back
    too, so a rejected skill can never leave a stray script behind.
    """
    root = before.root
    installed: List[Dict[str, str]] = []
    assets: List[str] = []
    rolled_back: List[Dict[str, str]] = []
    unprotected: List[str] = []

    after = Snapshot(root, before.home).capture()
    changed = sorted(
        path for path, digest in after.files.items() if before.files.get(path) != digest
    )
    # A file the run deleted is a change too — the distiller has no business
    # removing skills, and losing one silently is worse than a bad edit.
    removed = sorted(path for path in before.files if path not in after.files)

    def restore(path: str, *, existed: bool, reason: str) -> None:
        if existed and path in before.unbacked:
            # Changed, but we never held a copy — say so rather than reporting
            # a rollback that did not happen.
            unprotected.append(path)
            return
        original = before.original(path) if existed else None
        if existed and original is None:
            unprotected.append(path)
            return
        if _restore(path, original, before.modes.get(path) if existed else None):
            rolled_back.append({"name": skill_paths.skill_name(path), "reason": reason})
        else:
            unprotected.append(path)

    # An archive move looks exactly like a deletion from the live tree PLUS an
    # identical file appearing under `.archive/` — and `.archive/` is inside the
    # snapshot on purpose. Reverting that half-move leaves the skill in BOTH
    # places. Observed on the first real consolidation run: the pass archived a
    # merged-away skill, the guard restored the original, and the library ended
    # up with a live copy and an archived copy of the same bytes.
    #
    # Matching on content, not on path, is what keeps the protection intact: a
    # child that simply deletes a skill produces no archived twin and is still
    # reverted. Only a move whose bytes landed in the archive is honoured.
    archive_root = os.path.join(root, ".archive") + os.sep
    archive_arrivals = {
        path: digest
        for path, digest in after.files.items()
        if path.startswith(archive_root)
        and path not in before.files
        and digest != "unreadable"
    }
    arrival_digests = set(archive_arrivals.values())
    archived: List[Dict[str, str]] = []
    moved_digests = set()

    for path in removed:
        digest = before.files.get(path)
        if digest and digest != "unreadable" and digest in arrival_digests:
            archived.append({"name": skill_paths.skill_name(path), "path": path})
            moved_digests.add(digest)
            continue
        restore(path, existed=True, reason="deleted")

    # The arrival side of an accepted move is the SAME bytes, relocated — not
    # new content the run authored. Judging it again would reject it: an
    # archived SKILL.md would be reported as a freshly installed skill, and its
    # `references/` would be reverted as an orphan whose owning skill "isn't
    # valid". Both were observed on the first real consolidation run.
    if moved_digests:
        changed = [
            path
            for path in changed
            if not (path in archive_arrivals and archive_arrivals[path] in moved_digests)
        ]

    # Decide each skill from its SKILL.md first, so its assets can follow it.
    rejected_skills: Dict[str, str] = {}
    skill_files = [p for p in changed if os.path.basename(p) == "SKILL.md"]
    for path in skill_files:
        name = skill_paths.skill_name(path)
        existed = path in before.files
        previous_text = _decode(before.original(path)) if existed else None
        owner = _owning_skill(path, root)

        # Read once and judge THOSE bytes. Validating a second read would let a
        # write landing in between leave content on disk that nothing checked,
        # while the report claimed it was installed.
        current_bytes = _read(path)
        current_text = _decode(current_bytes) or ""

        reason: Optional[str] = None
        if not skill_paths.is_personal_skill(path, root):
            reason = "outside_write_root"
        elif current_bytes is None or _digest(current_bytes) != after.files.get(path):
            # The file moved under us mid-verification.
            reason = "changed_during_verification"
        elif existed and path in before.unbacked:
            # Without the pre-run text we cannot tell whether a pin was
            # stripped, so this edit cannot be judged safe.
            reason = "no_rollback_baseline"
        elif _is_pinned(name, previous_text if existed else current_text):
            # For a brand-new file the written text is the only evidence: an
            # unattended run must not be able to CREATE a curator-protected
            # skill that later edits are then blocked from fixing.
            reason = "pinned"
        else:
            problems = validate_skill._validate(current_text)
            if problems:
                reason = "invalid: " + "; ".join(problems)

        if reason is None:
            installed.append({"name": name, "path": path})
            continue
        if owner:
            rejected_skills[owner] = reason
        restore(path, existed=existed, reason=reason)

    for path in changed:
        if os.path.basename(path) == "SKILL.md":
            continue
        existed = path in before.files
        owner = _owning_skill(path, root)
        if owner is None:
            # A loose file directly under the skills root belongs to no skill.
            restore(path, existed=existed, reason="not_part_of_a_skill")
        elif not skill_paths.is_personal_skill(os.path.join(owner, "SKILL.md"), root):
            restore(path, existed=existed, reason="outside_write_root")
        elif owner in rejected_skills:
            restore(path, existed=existed, reason=rejected_skills[owner])
        elif _skill_dir_is_pinned(owner, before):
            restore(path, existed=existed, reason="pinned")
        elif not _has_valid_skill(owner):
            # Otherwise a run could drop `foo/scripts/run.py` without ever
            # writing `foo/SKILL.md`, leaving executable content that belongs
            # to no skill and that nothing validated.
            restore(path, existed=existed, reason="no_valid_owning_skill")
        else:
            assets.append(path)

    out_of_scope = [
        path for path, digest in after.watched.items() if before.watched.get(path) != digest
    ]
    _record_patches(installed, before.patch_counts)

    report: Dict[str, Any] = {
        "installed": installed,
        "assets": sorted(assets),
        "rolled_back": rolled_back,
        "out_of_scope_writes": sorted(out_of_scope),
    }
    if archived:
        report["archived"] = archived
    if unprotected:
        report["unprotected"] = sorted(set(unprotected))
    return report


def _record_patches(installed: List[Dict[str, str]], before_counts: Dict[str, int]) -> None:
    """Count each installed skill once.

    If the child session loaded this plugin's PostToolUse hook, that hook has
    already recorded the patch; counting again here would make an
    actively-maintained skill look busier than it is and skew the curator's
    idle clock. Compare the counter to its pre-run value to tell.
    """
    if usage_store is None or not installed:
        return
    after_counts = _patch_counts()
    events = []
    for item in installed:
        name = item["name"]
        if after_counts.get(name, 0) > before_counts.get(name, 0):
            continue  # the child's own hook already counted this write
        events.append((name, "patch", "agent"))
    if not events:
        return
    try:
        usage_store.apply_events(events)
    except Exception:
        pass


def revert_to(before: Snapshot) -> List[str]:
    """Put the skill tree back exactly as `before` found it.

    Used when a run produced no usable verdict. Unlike `verify`, nothing is
    judged or installed: every difference is undone, because a run that could
    not report what it did has no standing to change the library.
    """
    reverted: List[str] = []
    after = Snapshot(before.root, before.home).capture()
    for path in sorted(set(after.files) | set(before.files)):
        existed = path in before.files
        if existed and after.files.get(path) == before.files.get(path):
            continue
        if existed and path in before.unbacked:
            continue  # no copy to put back; verify() reports it as unprotected
        original = before.original(path) if existed else None
        if existed and original is None:
            continue
        if _restore(path, original, before.modes.get(path) if existed else None):
            reverted.append(path)
    return reverted


def snapshot(
    root: Optional[str] = None, home: Optional[str] = None, store: Optional[str] = None
) -> Snapshot:
    return Snapshot(root or skill_paths.personal_skills_root(), home, store).capture()


def stamp_provenance(installed: List[Dict[str, str]]) -> None:
    """Mark installed skills as distilled so the curator can tell them apart.

    Stamping writes to the file after the guard's only validation, so the
    result is re-checked: injected metadata can push a file that was exactly at
    the size limit over it, and an interrupted write can truncate it. If the
    stamp broke the skill, the pre-stamp text goes back.
    """
    for item in installed:
        path = item.get("path")
        if not path:
            continue
        original = _read(path)
        text = _decode(original)
        if text is None:
            continue
        try:
            validate_skill._stamp_provenance(path, text)
        except Exception:
            continue
        stamped = _decode(_read(path))
        if stamped is None or validate_skill._validate(stamped):
            _restore(path, original)

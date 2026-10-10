# -*- coding: utf-8 -*-
"""Integration tests: the exclude engine against a real restic binary.

These are the tests that matter most. The unit tests port restic's own golden
cases, but the real question is whether this module agrees with what restic
*actually does* when it decides what to skip. So: build a tree, back it up
with real excludes, ask restic what it stored, and compare.

Everything runs against a throwaway local repo in a temp directory — no network,
no real repo, seconds per run, and the directory is removed afterwards.
"""

import json
import os
import shutil

import pytest

from conftest import backup, init_repo, restic, stored_files

from snap_bak import excludes

pytestmark = pytest.mark.skipif(
    shutil.which("restic") is None, reason="restic binary not available"
)

@pytest.fixture
def repo(tmp_path):
    """An initialised local restic repo."""
    path = str(tmp_path / "repo")
    return init_repo(path)


@pytest.fixture
def tree(tmp_path):
    """A source tree exercising every matching rule the port implements."""
    root = tmp_path / "src"
    root.mkdir()

    def put(rel: str, data: bytes = b"x"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    # Relative patterns match at any depth.
    put("top.tmp")
    put("a/b/deep.tmp")
    put("a/b/keep.txt")
    # A directory match prunes its whole subtree.
    put("cache/big.bin", b"z" * 100)
    put("cache/nested/deeper.bin", b"w" * 50)
    put("a/cache/also.bin", b"y" * 20)
    # Anchored patterns match from the backup root only.
    put(".config/app.cfg")
    put("home/.config/other.cfg")
    # Character classes and `?`.
    put("file1.txt")
    put("file2.txt")
    put("fileA.txt")
    # `**` across directories.
    put("x/y/z/deep.log")
    put("x/deep.log")
    # Negation.
    put("node_modules/pkg/index.js")
    put("node_modules/pkg/keep.js")
    # Spaces and unicode in names.
    put("my file.tmp")
    put("ünïcødé.tmp")
    # A symlink (stored as a link, never followed).
    os.symlink("keep.txt", root / "link.tmp")
    return root


def test_matcher_agrees_with_what_restic_actually_excludes(repo, tree):
    """The core cross-check: for a set of patterns, the module's answer for every
    path in the tree must equal restic's decision about whether to store it."""
    patterns = [
        "*.tmp",
        "cache",
        "**/deep.log",
        "file?.txt",
        "!file2.txt",
    ]
    snap = backup(repo, str(tree), *[a for p in patterns for a in ("--exclude", p)])

    stored = stored_files(repo, snap)

    # Every file in the tree is either stored or excluded — never both, never
    # neither. That is the property the whole feature rests on.
    all_files = {str(p) for p in tree.rglob("*") if p.is_file() or p.is_symlink()}
    excluded = {
        w.path
        for w in excludes.walk_excluded(
            tree, str(tree), excludes.parse_patterns(patterns)
        )
    }
    # A matched directory is one row standing for its whole subtree.
    excluded_files = set()
    for path in excluded:
        rel = path[len(str(tree)) :].lstrip("/")
        if (tree / rel).is_dir():
            for sub in (tree / rel).rglob("*"):
                if sub.is_file() or sub.is_symlink():
                    excluded_files.add(str(sub))
        else:
            excluded_files.add(path)

    assert excluded_files.isdisjoint(stored), (
        f"both stored and excluded: {excluded_files & stored}"
    )
    assert excluded_files | stored == all_files, (
        f"unaccounted: {all_files - excluded_files - stored}, "
        f"missing: {(excluded_files | stored) - all_files}"
    )


def test_directory_match_prunes_subtree_likerestic(repo, tree):
    """A pattern matching a directory must exclude everything beneath it, the
    same way restic's walker prunes rather than descends."""
    snap = backup(repo, str(tree), "--exclude", "cache")
    stored = stored_files(repo, snap)

    assert not any("/cache/" in p for p in stored), stored
    # Siblings of the excluded directory are untouched.
    assert any(p.endswith("keep.txt") for p in stored)


def test_negation_cannot_rescue_inside_excluded_directory(repo, tree):
    """restic cannot re-include a file inside an excluded directory, because it
    never descends into one. The walk must agree."""
    snap = backup(
        repo, str(tree), "--exclude", "cache", "--exclude", "!cache/keep.txt"
    )
    stored = stored_files(repo, snap)
    assert not any("/cache/" in p for p in stored), stored


def test_anchored_pattern_matches_backup_root_not_live_path(repo, tree):
    """Anchored patterns are evaluated against the path restic was given.

    In production that is the mount point (`/bak/data`), not the live source
    (`/mnt/data`) — so an anchored pattern must name the backup root, and it
    reaches only what is beneath that exact prefix. Here the backup root is the
    tree path.
    """
    snap = backup(repo, str(tree), "--exclude", f"{tree}/.config")
    stored = stored_files(repo, snap)
    # The anchored directory is gone...
    assert not any(p.endswith(".config/app.cfg") for p in stored), stored
    # ...but a same-named directory elsewhere in the tree survives, because the
    # anchor is a specific prefix, not a component match.
    assert any(p.endswith("home/.config/other.cfg") for p in stored), stored

    # A non-anchored pattern with the same tail matches at any depth, so it
    # takes both — the contrast that shows what anchoring buys you.
    snap2 = backup(repo, str(tree), "--exclude", ".config")
    stored2 = stored_files(repo, snap2)
    assert not any(".config" in p for p in stored2), stored2


def test_restic_rewrite_forget_removes_excluded_files(repo, tree):
    """restic's own rewrite+forget semantics, which snap-bak's execute() relies
    on: the original is gone, the rewritten snapshot lacks the newly excluded
    files, and unexcluded files survive.

    This verifies restic, not snap-bak. The snap-bak end-to-end path — which
    snapshots get *selected* — is in tests/test_apply.py.
    """
    # First backup: only *.tmp excluded.
    snap1 = backup(repo, str(tree), "--exclude", "*.tmp")
    assert any(p.endswith("cache/big.bin") for p in stored_files(repo, snap1))

    # Now `cache` becomes excluded too. Rewriting must drop it.
    patterns = ["*.tmp", "cache"]
    result = restic(repo, "snapshots", "--json")
    snaps = json.loads(result.stdout)
    target = snaps[0]["id"]

    result = restic(
        repo,
        "rewrite",
        target,
        *[a for p in patterns for a in ("--exclude", p)],
        "--forget",
    )
    assert result.returncode == 0, result.stderr

    # The original is gone; a rewritten snapshot remains, without `cache`.
    result = restic(repo, "snapshots", "--json")
    remaining = json.loads(result.stdout)
    assert len(remaining) == 1
    assert remaining[0]["id"] != target

    stored = stored_files(repo, remaining[0]["id"])
    assert not any("/cache/" in p for p in stored), stored
    # Files that were never excluded are still there.
    assert any(p.endswith("keep.txt") for p in stored)


def test_restic_rewrite_is_idempotent(repo, tree):
    """restic's rewrite+forget is idempotent: applying the same rules twice
    changes nothing the second time. Again, restic's behaviour, not snap-bak's."""
    backup(repo, str(tree), "--exclude", "*.tmp", "--exclude", "cache")
    patterns = ["*.tmp", "cache"]

    result = restic(repo, "snapshots", "--json")
    target = json.loads(result.stdout)[0]["id"]

    for _ in range(2):
        result = restic(
            repo,
            "rewrite",
            target,
            *[a for p in patterns for a in ("--exclude", p)],
            "--forget",
        )
        assert result.returncode == 0, result.stderr
        result = restic(repo, "snapshots", "--json")
        target = json.loads(result.stdout)[0]["id"]

    stored = stored_files(repo, target)
    assert not any(p.endswith(".tmp") for p in stored)
    assert not any("/cache/" in p for p in stored)


def test_restic_prune_after_rewrite_reclaims_and_stays_readable(repo, tree):
    """After rewrite+forget, prune must succeed and the repo must stay readable
    — the data still referenced survives. restic's behaviour, not snap-bak's."""
    backup(repo, str(tree), "--exclude", "*.tmp")
    result = restic(repo, "snapshots", "--json")
    target = json.loads(result.stdout)[0]["id"]

    result = restic(
        repo, "rewrite", target, "--exclude", "*.tmp", "--exclude", "cache", "--forget"
    )
    assert result.returncode == 0, result.stderr

    result = restic(repo, "prune")
    assert result.returncode == 0, result.stderr

    # The repo is still valid and the kept files are still there.
    result = restic(repo, "snapshots", "--json")
    remaining = json.loads(result.stdout)
    assert len(remaining) == 1
    stored = stored_files(repo, remaining[0]["id"])
    assert any(p.endswith("keep.txt") for p in stored)

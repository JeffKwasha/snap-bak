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
from concurrent.futures import ProcessPoolExecutor

import pytest
from conftest import backup, init_repo, restic, stored_files

from snap_bak import excludes, report

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


def walk(root, base, parsed):
    """Walk the way the CLI does: through a process pool.

    The agreement this test exists to prove is only worth anything on the path
    production takes. The pool path went unchecked for a while and shipped a bug
    that truncated every emitted path to its basename, which this caught the
    moment it was pointed at the real walk.
    """
    with ProcessPoolExecutor(
        max_workers=4,
        initializer=excludes._init_worker,
        initargs=(parsed,),
    ) as pool:
        yield from excludes.walk_excluded(
            root, base, parsed, attribute_rules=True,
            pool=pool, skip=str(root / "skipme"), top_level=True,
        )


def test_the_live_walk_and_the_repository_report_the_same_numbers(repo, tree):
    """The report's whole premise is that two columns can be compared.

    They only can if the filesystem walk and the restic listing count the same
    things the same way — files under the same rules, bytes to the same total.
    Before this was pinned, a symlink was 11 bytes to the walk and 0 to restic,
    and a matched directory's nested subdirectories were counted as files by one
    side and not the other, so two columns of the same row disagreed.

    Backed up with nothing excluded, so the *live* rules being evaluated are the
    ones whose effect both sides are asked about.
    """
    patterns = ["*.tmp", "cache", "**/deep.log", "file?.txt", "!file2.txt"]
    snap = backup(repo, str(tree))

    parsed = excludes.parse_patterns(patterns)
    base = str(tree)

    walked = excludes.ExclusionSet()
    for w in walk(tree, base, parsed):
        walked.add(excludes.LIVE, w.path, w.size, w.is_dir, w.rule, w.files)

    listing = restic(repo, "ls", "--long", snap).stdout
    stored = excludes.ExclusionSet()
    excludes.collect_from_listing(
        excludes.parse_ls_long(listing), "abc12345", parsed,
        into=stored, attribute_rules=True,
    )

    # Same rules, same numbers, from either side.
    assert walked.by_rule() == stored.by_rule()
    assert walked.total_files == stored.total_files
    assert walked.total_size == stored.total_size
    assert {w.path for w in walked.ordered_entries()} == {
        e.path for e in stored.ordered_entries()
    }

    # And the grid they feed shows the same figure in both columns.
    sheet = report.build(
        "Test", base, patterns, [("live", walked), ("repo", stored)]
    )
    for row in sheet.rows:
        assert row.cells[excludes.LIVE] == row.cells["abc12345"], row.label


def test_a_symlinked_directory_is_one_file_worth_nothing(repo, tree):
    """restic stores a link as a node of size 0 and `ls --long` reports it that
    way. Charging it the length of its target would make the live column and the
    repository column disagree about the same file."""
    link = tree / "dirlink"
    os.symlink("a/b", link, target_is_directory=True)
    snap = backup(repo, str(tree))

    found = excludes.ExclusionSet()
    excludes.collect_from_listing(
        excludes.parse_ls_long(restic(repo, "ls", "--long", snap).stdout),
        "s1", excludes.parse_patterns(["dirlink"]), into=found,
    )
    parsed = excludes.parse_patterns(["dirlink"])
    walked = excludes.ExclusionSet()
    for w in walk(tree, str(tree), parsed):
        walked.add(excludes.LIVE, w.path, w.size, w.is_dir, w.rule, w.files)

    assert walked.total_files == found.total_files == 1
    assert walked.total_size == found.total_size == 0


def test_a_hardlinked_pair_counts_once_per_path_in_both_columns(repo, tree):
    """restic's `ls` reports each hardlinked path at full size, so counting once
    per path is what keeps the two columns equal. The figure still over-states
    what `prune` reclaims, by exactly the shared block — deliberately, because
    agreeing with restic matters more than matching post-prune `stats` here."""
    (tree / "hard.tmp").write_bytes(b"h" * 40)
    os.link(tree / "hard.tmp", tree / "hard2.tmp")
    snap = backup(repo, str(tree))

    parsed = excludes.parse_patterns(["hard.tmp", "hard2.tmp"])
    walked = excludes.ExclusionSet()
    for w in excludes.walk_excluded(tree, str(tree), parsed):
        walked.add(excludes.LIVE, w.path, w.size, w.is_dir, w.rule, w.files)
    stored = excludes.ExclusionSet()
    excludes.collect_from_listing(
        excludes.parse_ls_long(restic(repo, "ls", "--long", snap).stdout),
        "s1", parsed, into=stored,
    )

    assert walked.total_files == stored.total_files == 2
    assert walked.total_size == stored.total_size == 80

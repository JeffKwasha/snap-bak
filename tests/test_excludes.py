# -*- coding: utf-8 -*-
"""Unit tests for snap_bak/excludes.py — restic's exclusion engine, ported.

The MATCH_* / CHILD_* / LIST_* tables below are restic's own golden cases from
`internal/filter/filter_test.go` (restic 0.19.1). They are ported verbatim so
this module cannot silently drift from what restic actually excludes: restic
decides exclusions with that engine, so any divergence would make every
inspection mode report the wrong files.
"""

import os

import pytest

from snap_bak import excludes
from snap_bak.excludes import BadPatternError


# --- restic matchTests (filter_test.go) ------------------------------------

MATCH_CASES = [
    ("", "", True),
    ("", "foo", True),
    ("", "/x/y/z/foo", True),
    ("*.go", "/foo/bar/test.go", True),
    ("*.c", "/foo/bar/test.go", False),
    ("*", "/foo/bar/test.go", True),
    ("**", "/foo/bar/test.go", True),
    ("foo*", "/foo/bar/test.go", True),
    ("bar*", "/foo/bar/test.go", True),
    ("/bar*", "/foo/bar/test.go", False),
    ("bar/*", "/foo/bar/test.go", True),
    ("baz/*", "/foo/bar/test.go", False),
    ("bar/test.go", "/foo/bar/test.go", True),
    ("bar/*.go", "/foo/bar/test.go", True),
    ("ba*/*.go", "/foo/bar/test.go", True),
    ("bb*/*.go", "/foo/bar/test.go", False),
    ("test.*", "/foo/bar/test.go", True),
    ("tesT.*", "/foo/bar/test.go", False),
    ("bar/*", "/foo/bar/baz", True),
    ("bar", "/foo/bar", True),
    ("/foo/bar", "/foo/bar", True),
    ("/foo/bar/", "/foo/bar", True),
    ("/foo/bar", "/foo/baz", False),
    ("/foo/bar", "/foo/baz/", False),
    ("/foo///bar", "/foo/bar", True),
    ("/foo/../bar", "/foo/bar", False),
    ("/foo/../bar", "/bar", True),
    ("/foo", "/foo/baz", True),
    ("/foo/", "/foo/baz", True),
    ("/foo/*", "/foo", False),
    ("/foo/*", "/foo/baz", True),
    ("bar", "/foo/bar/baz", True),
    ("bar", "/foo/bar/test.go", True),
    ("/foo/*test.*", "/foo/bar/test.go", False),
    ("/foo/*/test.*", "/foo/bar/test.go", True),
    ("/foo/*/bar/test.*", "/foo/bar/test.go", False),
    ("/*/*/bar/test.*", "/foo/bar/test.go", False),
    ("/*/*/bar/test.*", "/foo/bar/baz/test.go", False),
    ("/*/*/baz/test.*", "/foo/bar/baz/test.go", True),
    ("/*/foo/bar/test.*", "/foo/bar/baz/test.go", False),
    ("/*/foo/bar/test.*", "/foo/bar/baz/test.go", False),
    ("/foo/bar/test.*", "bar/baz/test.go", False),
    ("/x/y/bar/baz/test.*", "bar/baz/test.go", False),
    ("/x/y/bar/baz/test.c", "bar/baz/test.go", False),
    ("baz/test.*", "bar/baz/test.go", True),
    ("baz/tesT.*", "bar/baz/test.go", False),
    ("test.go", "bar/baz/test.go", True),
    ("*.go", "bar/baz/test.go", True),
    ("*.c", "bar/baz/test.go", False),
    ("sdk", "/foo/bar/sdk", True),
    ("sdk", "/foo/bar/sdk/test/sdk_foo.go", True),
    (
        "sdk/*/cpp/*/*vars*.html",
        "/usr/share/doc/libreoffice/sdk/docs/cpp/ref/a00517.html",
        False,
    ),
    ("foo/**/bar/*.go", "/home/user/foo/work/special/project/bar/test.go", True),
    ("foo/**/bar/*.go", "/home/user/foo/bar/test.go", True),
    ("foo/**/bar/*.go", "x/foo/bar/test.go", True),
    ("foo/**/bar/*.go", "foo/bar/test.go", True),
    ("foo/**/bar/*.go", "foo/bar/baz/bar/test.go", True),
    ("foo/**/bar/*.go", "/home/user/foo/test.c", False),
    ("foo/**/bar/*.go", "bar/foo/main.go", False),
    ("foo/**/bar/*.go", "/foo/bar/main.go", True),
    ("foo/**/bar/*.go", "bar/main.go", False),
    ("foo/**/bar", "/home/user/foo/x/y/bar", True),
    ("foo/**/bar", "/home/user/foo/x/y/bar/main.go", True),
    ("foo/**/bar/**/x", "/home/user/foo/bar/x", True),
    ("foo/**/bar/**/x", "/home/user/foo/blaaa/blaz/bar/shared/work/x", True),
    ("user/**/important*", "/home/user/work/x/y/hidden/x", False),
    ("user/**/important*", "/home/user/work/x/y/importantdoc", True),
    ("user/**/hidden*/**/c", "/home/user/work/x/y/hidden/z/a/b/c", True),
    # Two `**` in one pattern, plus a partially-matching segment.
    ("user/**/hidden*/**/c", "/home/user/work/x/y/nope/z/a/b/c", False),
    ("a/**/b/**/c", "/a/b/c", True),
    ("a/**/b/**/c", "/a/x/b/y/c", True),
    ("a/**/b/**/c", "/a/x/b/y/d", False),
    ("**/a/**/b/**/c", "/q/a/r/b/t/c", True),
    ("c:/foo/*test.*", "c:/foo/bar/test.go", False),
    ("c:/foo", "c:/foo/bar", True),
    ("c:/foo/", "c:/foo/bar", True),
    ("c:/foo/*/test.*", "c:/foo/bar/test.go", True),
    ("c:/foo/*/bar/test.*", "c:/foo/bar/test.go", False),
]


@pytest.mark.parametrize("pattern,path,expected", MATCH_CASES)
def test_match(pattern, path, expected):
    assert excludes.match(pattern, path) is expected


# --- restic childMatchTests (filter_test.go) ------------------------------

CHILD_CASES = [
    ("", "", True),
    ("", "/foo", True),
    ("", "/x/y/z/foo", True),
    ("foo/bar", "/foo", True),
    ("baz/bar", "/foo", True),
    ("foo", "/foo/bar", True),
    ("bar", "/foo", True),
    ("baz", "/foo/bar", True),
    ("*", "/foo", True),
    ("*", "/foo/bar", True),
    ("/foo/bar", "/foo", True),
    ("/foo/bar/baz", "/foo", True),
    ("/foo/bar/baz", "/foo/bar", True),
    ("/foo/bar/baz", "/foo/baz", False),
    ("/foo/**/baz", "/foo/bar/baz", True),
    ("/foo/**/baz", "/foo/bar/baz/blah", True),
    ("/foo/**/qux", "/foo/bar/baz/qux", True),
    ("/foo/**/qux", "/foo/bar/baz", True),
    ("/foo/**/qux", "/foo/bar/baz/boo", True),
    ("/foo/**", "/foo/bar/baz", True),
    ("/foo/**", "/foo/bar", True),
    ("foo/**/bar/**/x", "/home/user/foo", True),
    ("foo/**/bar/**/x", "/home/user/foo/bar", True),
    ("foo/**/bar/**/x", "/home/user/foo/blaaa/blaz/bar/shared/work/x", True),
    ("/foo/*/qux", "/foo/bar", True),
    ("/foo/*/qux", "/foo/bar/boo", False),
    ("/foo/*/qux", "/foo/bar/boo/xx", False),
    ("/baz/bar", "/foo", False),
    ("/foo", "/foo/bar", True),
    ("/*", "/foo", True),
    ("/*", "/foo/bar", True),
    ("/foo", "/foo/bar", True),
    ("/**", "/foo", True),
    ("/*/**", "/foo", True),
    ("/*/**", "/foo/bar", True),
    ("/*/bar", "/foo", True),
    ("/bar/*", "/foo", False),
    ("/foo/*/baz", "/foo/bar", True),
    ("/foo/*/baz", "/foo/baz", True),
    ("/foo/*/baz", "/bar/baz", False),
    ("/**/*", "/foo", True),
    ("/**/bar", "/foo/bar", True),
]


@pytest.mark.parametrize("pattern,path,expected", CHILD_CASES)
def test_child_match(pattern, path, expected):
    assert excludes.child_match(pattern, path) is expected


# --- restic filterListTests (filter_test.go) ------------------------------

LIST_CASES = [
    ([], "/foo/bar/test.go", False, False),
    (["*.go"], "/foo/bar/test.go", True, True),
    (["*.go"], "/foo/bar", False, True),
    (["*.c"], "/foo/bar/test.go", False, True),
    (["*.go", "*.c"], "/foo/bar/test.go", True, True),
    (["*"], "/foo/bar/test.go", True, True),
    (["x"], "/foo/bar/test.go", False, True),
    (["?"], "/foo/bar/test.go", False, True),
    (["?", "x"], "/foo/bar/x", True, True),
    (["/*/*/bar/test.*"], "/foo/bar/test.go", False, False),
    (["/*/*/bar/test.*"], "/foo/bar/bar", False, True),
    (["/*/*/bar/test.*", "*.go"], "/foo/bar/test.go", True, True),
    (["", "*.c"], "/foo/bar/test.go", False, True),
    (["!**", "*.go"], "/foo/bar/test.go", True, True),
    (["!**", "*.c"], "/foo/bar/test.go", False, True),
    (["/foo/*/test.*", "!*.c"], "/foo/bar/test.c", False, False),
    (["/foo/*/test.*", "!*.c"], "/foo/bar/test.go", True, True),
    (["/foo/*/*", "!test.*", "*.c"], "/foo/bar/test.go", False, True),
    (["/foo/*/*", "!test.*", "*.c"], "/foo/bar/test.c", True, True),
    (["/foo/*/*", "!test.*", "*.c"], "/foo/bar/file.go", True, True),
    # A `**` followed by more pattern. restic's ChildMatch answers True for any
    # relative pattern regardless of what matched, so a path that does not match
    # can still have matching children — the walk, not this flag, is what prunes.
    (["a/**/b/**/c"], "/a/x/b/y/c", True, True),
    (["a/**/b/**/c"], "/a/x/b/y/d", False, True),
    (["/**/*", "!/foo", "/foo/*", "!/foo/bar"], "/foo/other/test.go", True, True),
    (["/**/*", "!/foo", "/foo/*", "!/foo/bar"], "/foo/bar", False, False),
    (["/**/*", "!/foo", "/foo/*", "!/foo/bar"], "/foo/bar/test.go", False, False),
    (
        ["/**/*", "!/foo", "/foo/*", "!/foo/bar", "/foo/bar/test*"],
        "/foo/bar/test.go/child",
        True,
        True,
    ),
    (["/foo/bar/*"], "/foo", False, True),
    (["/foo/bar/*", "!/foo/bar/[a-m]*"], "/foo", False, True),
    (["/foo/**/test.c"], "/foo/bar/foo/bar/test.c", True, True),
    (["/foo/*/test.c"], "/foo/bar/foo/bar/test.c", False, False),
]


@pytest.mark.parametrize("patterns,path,match,child", LIST_CASES)
def test_list_match(patterns, path, match, child):
    parsed = excludes.parse_patterns(patterns)
    assert excludes.list_match(parsed, path) == (match, child)


# Negation of a `**` pattern: a matched directory is re-included, and since
# restic's ChildMatch answers True for any relative pattern, children may still
# match too. These are the shapes users rely on when re-including a subdir of an
# excluded tree (see the `excluded` cases below for the match-only behaviour).
@pytest.mark.parametrize("patterns,path,expected", [
    (["**/cache", "!cache/keep"], "/bak/home/x/cache", True),
    (["**/cache", "!cache/keep"], "/bak/home/x/cache/keep", False),
    (["**/cache", "!cache/keep"], "/bak/home/x/cache/other", True),
    (["**/cache"], "/bak/home/x/cache", True),
    (["!cache"], "/bak/home/x/cache", False),
])
def test_list_match_negation_with_double_wildcard(patterns, path, expected):
    matched, _child = excludes.list_match(excludes.parse_patterns(patterns), path)
    assert matched is expected


@pytest.mark.parametrize("patterns,path,expected", [
    (["*.go"], "/foo/bar/test.go", True),
    (["*.c"], "/foo/bar/test.go", False),
    (["Trash"], "/bak/home/user/Trash", True),
    (["Trash"], "/bak/home/user/Trash/x", True),
    (["Trash"], "/bak/home/user/trash", False),
    ([], "/foo/bar", False),
    # Re-including inside an excluded tree. restic's walker prunes an excluded
    # directory without descending, so a negated child pattern cannot rescue
    # anything beneath it; the per-path matcher agrees for the dir itself.
    (["**/cache", "!cache/keep"], "/bak/home/x/cache", True),
    (["**/cache", "!cache/keep"], "/bak/home/x/cache/keep", False),
])
def test_excluded(patterns, path, expected):
    assert excludes.excluded(excludes.parse_patterns(patterns), path) is expected


@pytest.mark.parametrize("pattern,path,expected", [
    ("*.tmp", "/bak/home/user/a.tmp", True),
    ("*.tmp", "/bak/home/user/a.tmpx", False),
    ("Trash", "/bak/home/user/Trash", True),
    ("Trash", "/bak/home/user/Trash/deep/x", True),
    ("Trash", "/bak/home/user/trash", False),
    ("user/**/node_modules", "/bak/home/user/x/node_modules", True),
    ("user/**/node_modules", "/bak/home/user/x/y/node_modules", True),
    ("**/node_modules", "/bak/home/node_modules", True),
    ("node_modules", "/bak/home/user/node_modules/a/b", True),
])
def test_realistic_patterns(pattern, path, expected):
    """Patterns as users actually write them, against backup-time paths."""
    assert excludes.excluded(excludes.parse_patterns([pattern]), path) is expected


# --- the memoised `**` expansion -----------------------------------------


class TestExpansionCache:
    """The `**` expansion is memoised so a large pattern set stays affordable.

    Every restic golden case above runs through it, but these pin the two ways
    a cache here can be wrong in ways the goldens do not catch: colliding keys,
    and an answer that depends on the path rather than only on the shape.
    """

    def test_a_warm_cache_answers_identically_to_a_cold_one(self):
        """The cache must not change any answer, only how long one takes."""
        pats = [
            "/a/**/b/**/c", "**/a/**/b/**/c", "user/**/hidden*/**/c",
            "**/node_modules/**", "*.tmp", "!keep.tmp", "**", "**/x/**/**/y",
        ]
        paths = [
            "/a/x/b/y/c", "/q/a/r/b/t/c", "/home/user/work/x/y/hidden/z/a/b/c",
            "/bak/E/node_modules/z.js", "/bak/E/a.tmp", "/bak/E/keep.tmp",
            "/bak/E/x/p/q/r/y", "/bak/E/deep/a/b/c/d/e",
        ]
        for raw in pats:
            parsed = excludes.parse_patterns([raw])
            cold = [excludes.list_match(parsed, p) for p in paths]
            for p in paths:  # warm it
                excludes.list_match(parsed, p)
            warm = [excludes.list_match(parsed, p) for p in paths]
            assert warm == cold, f"{raw!r} changed answer once cached"

    def test_matching_depends_on_segment_values_not_only_their_count(self):
        """Why the cache stores expansions and not results.

        `/bak/E/a.tmp` and `/bak/E/b.txt` have the same pattern, the same
        segment count and the same depth — and opposite answers. A cache keyed on
        (pattern, segment count) would return one answer for both.
        """
        pats = excludes.parse_patterns(["*.tmp"])
        a = list(excludes.split_path("/bak/E/a.tmp"))
        b = list(excludes.split_path("/bak/E/b.txt"))
        assert len(a) == len(b)
        assert excludes._match_parts(pats[0], a) is True
        assert excludes._match_parts(pats[0], b) is False

    def test_mid_expansion_children_do_not_collide(self):
        """The key uses the parts, not (original, part count).

        Expanding `/a/**/b/**/c` produces six-part children that share its
        original text; keying on the text alone returned one child's shapes for
        the other and the pattern silently stopped matching.
        """
        pats = excludes.parse_patterns(["/a/**/b/**/c"])
        excludes.clear_matcher_cache()
        assert excludes.excluded(pats, "/a/x/b/y/c") is True

    def test_clear_is_observable(self):
        pats = excludes.parse_patterns(["/a/**/b/**/c"])
        excludes.list_match(pats, "/a/x/b/y/c")
        assert excludes._RESOLVED_CACHE
        excludes.clear_matcher_cache()
        assert excludes._RESOLVED_CACHE == {}

    def test_cache_is_bounded(self):
        """An unbounded cache would grow for every distinct depth a walk meets."""
        assert excludes._RESOLVED_LIMIT <= 100_000


# --- restic TestInvalidStrs / TestInvalidPattern -------------------------


def test_empty_path_rejected():
    with pytest.raises(BadPatternError):
        excludes.match("test", "")
    with pytest.raises(BadPatternError):
        excludes.child_match("test", "")
    with pytest.raises(BadPatternError):
        excludes.list_match(excludes.parse_patterns(["test"]), "")


def test_invalid_pattern_raises():
    with pytest.raises(BadPatternError):
        excludes.list_match(excludes.parse_patterns(["test/["]), "test/example")
    with pytest.raises(BadPatternError):
        excludes.list_match(excludes.parse_patterns(["test/**/["]), "test/example")


def test_validate_patterns_reports_restic_rejects():
    assert excludes.validate_patterns(["*.go", "**/cache"]) == []
    assert excludes.validate_patterns(["test/["]) == ["test/["]
    assert excludes.validate_patterns(["test/**/["]) == ["test/**/["]


def test_validate_patterns_accepts_leading_bang():
    # restic's validator matches each part against itself after the negation
    # prefix is stripped, so `!test/[` is reported as invalid.
    assert excludes.validate_patterns(["!*.go", "!**/cache"]) == []
    assert excludes.validate_patterns(["!test/["]) == ["!test/["]


# --- Go filepath.Match specifics -----------------------------------------


@pytest.mark.parametrize("pattern,name,expected", [
    ("*.go", "test.go", True),
    ("*.go", "test.gox", False),
    ("?.go", "a.go", True),
    ("?.go", "ab.go", False),
    ("[abc].txt", "b.txt", True),
    ("[abc].txt", "d.txt", False),
    ("[a-c].txt", "c.txt", True),
    ("[^a-c].txt", "d.txt", True),
    ("[^a-c].txt", "b.txt", False),
    ("[^a-c].txt", "!.txt", True),
# Go (and therefore restic) negates a class with '^' only; '!' is an
        # ordinary member of the set.
    ("[!a-c].txt", "d.txt", False),
    ("[!a-c].txt", "b.txt", True),  # '!' is an ordinary class member
    ("[!a-c].txt", "!.txt", True),  # so a leading '!' matches literally
    (r"\*.go", "*.go", True),
    (r"\*.go", "x.go", False),
    ("*", "", True),
    ("", "", True),
    ("", "x", False),
])
def test_segment_match(pattern, name, expected):
    assert excludes.segment_match(pattern, name) is expected


def test_segment_match_rejects_unterminated_class():
    # Go's filepath.Match("[", ...) is a bad pattern, not a literal.
    with pytest.raises(BadPatternError):
        excludes.segment_match("[", "[")


def test_star_does_not_cross_separator():
    # fnmatch would return True here; restic (Go) must not.
    assert excludes.segment_match("*", "a/b") is False
    assert excludes.segment_match("*.go", "sub/test.go") is False


@pytest.mark.parametrize("raw,expected", [
    ("", "."),
    (".", "."),
    ("/foo/bar", "/foo/bar"),
    ("/foo/bar/", "/foo/bar"),
    ("/foo//bar", "/foo/bar"),
    ("/foo/./bar", "/foo/bar"),
    ("/foo/../bar", "/bar"),
    ("/../bar", "/bar"),
    ("foo/../bar", "bar"),
    ("../bar", "../bar"),
    ("a/b/../../..", ".."),
])
def test_clean_path(raw, expected):
    assert excludes.clean_path(raw) == expected


# --- pattern file parsing -------------------------------------------------


def test_read_pattern_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SNAPBAK_TEST_DIR", "/data/cache")
    f = tmp_path / "job.ignore"
    f.write_text(
        "# a comment\n"
        "\n"
        "   *.tmp   \n"
        "$SNAPBAK_TEST_DIR\n"
        "$$literal\n"
        "  # indented comment\n"
        "Trash\n"
    )
    assert excludes.read_pattern_file(f) == [
        "*.tmp",
        "/data/cache",
        "$literal",
        "Trash",
    ]


def test_expand_env_edge_cases(monkeypatch):
    monkeypatch.setenv("SNAPBAK_TEST_X", "value")
    monkeypatch.delenv("SNAPBAK_TEST_MISSING", raising=False)
    assert excludes._expand_env("$SNAPBAK_TEST_X") == "value"
    assert excludes._expand_env("${SNAPBAK_TEST_X}") == "value"
    assert excludes._expand_env("$$SNAPBAK_TEST_X") == "$SNAPBAK_TEST_X"
    assert excludes._expand_env("$SNAPBAK_TEST_MISSING") == ""
    assert excludes._expand_env("cost: 5$") == "cost: 5$"


def test_patterns_for_job_merges_ignore_file_then_excludes(tmp_path):
    class FakeJob:
        ignore_file = str(tmp_path / "j.ignore")
        excludes = ["*.cache"]

    (tmp_path / "j.ignore").write_text("# c\n*.tmp\n")
    assert excludes.patterns_for_job(FakeJob()) == ["*.tmp", "*.cache"]


# --- lint -----------------------------------------------------------------


def test_lint_flags_invalid_pattern():
    issues = excludes.lint(["test/["], "/bak/data")
    assert [i.kind for i in issues] == ["invalid"]


def test_lint_flags_anchored_pattern_outside_backup_root():
    issues = excludes.lint(["/home/user/.cache"], "/bak/data")
    assert [i.kind for i in issues] == ["outside_root"]
    assert "outside the backup root" in issues[0].message


def test_lint_accepts_relative_and_rooted_patterns():
    assert excludes.lint(["*.tmp", "Trash"], "/bak/data") == []
    assert excludes.lint(["/bak/data/.cache"], "/bak/data") == []
    assert excludes.lint(["/bak/data/**/x"], "/bak/data") == []


def test_lint_ignores_negated_patterns():
    # A negated pattern cannot exclude anything, so "can never match" does not
    # apply to it — but a malformed one is still worth reporting.
    assert excludes.lint(["!/home/user/x"], "/bak/data") == []


def test_lint_checks_negated_patterns_for_syntax():
    issues = excludes.lint(["!test/["], "/bak/data")
    assert [i.kind for i in issues] == ["invalid"]


def test_lint_reports_every_offending_pattern():
    issues = excludes.lint(["/home/user/x", "/var/y", "*.tmp"], "/bak/data")
    assert sorted(i.pattern for i in issues) == ["/home/user/x", "/var/y"]
    assert all(i.kind == "outside_root" for i in issues)


# --- filesystem walk ------------------------------------------------------


def _make_tree(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "keep.txt").write_text("keep")
    (root / "a.tmp").write_text("x" * 10)
    (root / "sub").mkdir()
    (root / "sub" / "b.tmp").write_text("y" * 20)
    (root / "sub" / "keep2.txt").write_text("keep")
    cache = root / "cache"
    cache.mkdir()
    (cache / "big.bin").write_text("z" * 100)
    (cache / "nested").mkdir()
    (cache / "nested" / "deep.bin").write_text("w" * 50)
    return root


def test_walk_excluded_uses_backup_root_paths(tmp_path):
    root = _make_tree(tmp_path / "src")
    found = dict(
        (p, (size, is_dir))
        for p, size, is_dir in excludes.walk_excluded(
            root, "/bak/data", excludes.parse_patterns(["*.tmp", "cache"])
        )
    )
    assert found == {
        "/bak/data/a.tmp": (10, False),
        "/bak/data/sub/b.tmp": (20, False),
        # A matched directory is one row carrying its whole subtree.
        "/bak/data/cache": (150, True),
    }


def test_walk_does_not_descend_into_excluded_directory(tmp_path):
    """An excluded directory yields exactly one row; its children are not
    listed individually (restic prunes rather than descending)."""
    root = _make_tree(tmp_path / "src")
    paths = [
        p
        for p, _size, _is_dir in excludes.walk_excluded(
            root, "/bak/E", excludes.parse_patterns(["cache"])
        )
    ]
    assert paths == ["/bak/E/cache"]


def test_subtree_size_counts_nested_files_not_symlink_targets(tmp_path):
    root = tmp_path / "d"
    (root / "sub").mkdir(parents=True)
    (root / "a").write_bytes(b"x" * 10)
    (root / "sub" / "b").write_bytes(b"y" * 20)
    link = root / "link"
    link.symlink_to(root / "a")
    link_len = link.lstat().st_size  # the link's own size, not its target's

    # a(10) + sub/b(20) + the symlink's own length. If the symlink were
    # followed, `a` would be counted twice (40 + link_len) instead.
    assert excludes._subtree_size(root) == 30 + link_len


def test_walk_excluded_empty_patterns_returns_nothing(tmp_path):
    root = _make_tree(tmp_path / "src")
    assert list(excludes.walk_excluded(root, "/bak/x", excludes.parse_patterns([]))) == []


def test_walk_excluded_missing_root_is_not_an_error(tmp_path):
    # A job whose source subvolume is gone must not crash the inspection.
    assert (
        list(
            excludes.walk_excluded(
                tmp_path / "nope", "/bak/x", excludes.parse_patterns(["*.tmp"])
            )
        )
        == []
    )


def test_walk_excluded_trailing_slash_root(tmp_path):
    (tmp_path / "d.tmp").write_text("abc")
    found = list(
        excludes.walk_excluded(tmp_path, "/bak/x/", excludes.parse_patterns(["*.tmp"]))
    )
    assert found == [("/bak/x/d.tmp", 3, False)]


def test_walk_excluded_does_not_follow_symlinked_dirs(tmp_path):
    """A symlinked directory must not be walked: restic stores the link, and
    following it would report files the repo does not contain."""
    root = tmp_path / "src"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.tmp").write_text("secret")
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    found = list(
        excludes.walk_excluded(root, "/bak/x", excludes.parse_patterns(["*.tmp"]))
    )
    assert found == []


# --- restic ls --long parsing --------------------------------------------


# Real `restic ls --long` output (restic 0.19.1): mode uid gid size date time path.
# Note the header line, spaces and unicode in paths, and that directories are
# identified by mode — restic appends no trailing slash.
LS_LONG = """\
snapshot ab40ce2e of [/tmp/src] at 2026-10-06 22:35:05.36255209 -0400 EDT by claude@tv filtered by []:
dtrwxrwxrwx     0     0      0 2026-10-06 22:35:02 /tmp
drwxr-xr-x   131  1015      0 2026-10-06 22:35:05 /tmp/src
-rw-rw-r--   131  1015      3 2026-10-06 22:35:05 /tmp/src/a file.tmp
-rw-rw-r--   131  1015      6 2026-10-06 22:35:05 /tmp/src/sub/dir with spaces.tmp
drwxr-xr-x   131  1015      0 2026-10-06 22:35:05 /tmp/src/cache
-rw-rw-r--   131  1015   3072 2026-10-06 22:35:05 /tmp/src/cache/big.bin
-rw-rw-r--   131  1015      5 2026-10-06 22:35:05 /tmp/src/keep.txt
-rw-rw-r--   131  1015      7 2026-10-06 22:35:05 /tmp/src/ünïcødé.tmp
"""


def test_parse_ls_long_real_format():
    got = list(excludes.parse_ls_long(LS_LONG))
    assert got == [
        ("/tmp", 0, True),
        ("/tmp/src", 0, True),
        ("/tmp/src/a file.tmp", 3, False),
        ("/tmp/src/sub/dir with spaces.tmp", 6, False),
        ("/tmp/src/cache", 0, True),
        ("/tmp/src/cache/big.bin", 3072, False),
        ("/tmp/src/keep.txt", 5, False),
        ("/tmp/src/ünïcødé.tmp", 7, False),
    ]


def test_parse_ls_long_ignores_garbage_lines():
    assert list(excludes.parse_ls_long("\nnot a listing\n")) == []
    # A line whose size column is not an integer is skipped, not guessed at.
    assert list(excludes.parse_ls_long("-rw-r--r-- 1 u notanint d t /x\n")) == []


def test_walk_does_not_follow_symlinked_dirs(tmp_path):
    """A symlinked directory is stored by restic as a link, so the walk must not
    descend into it: reporting `outside/secret.tmp` would name a file the repo
    does not contain."""
    root = tmp_path / "src"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.tmp").write_text("secret")
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    assert (
        list(
            excludes.walk_excluded(root, "/bak/x", excludes.parse_patterns(["*.tmp"]))
        )
        == []
    )


def test_walk_excludes_symlinked_dir_by_name_without_descending(tmp_path):
    """A pattern matching the link's own name excludes the link, priced by its
    own length — the target's contents stay out of the report."""
    root = tmp_path / "src"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.tmp").write_text("secret" * 100)
    root.mkdir()
    link = root / "cache"
    link.symlink_to(outside, target_is_directory=True)

    found = list(
        excludes.walk_excluded(root, "/bak/x", excludes.parse_patterns(["cache"]))
    )
    assert found == [("/bak/x/cache", len(str(outside)), False)]


def test_walk_prices_matched_symlink_by_its_own_size(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    target = tmp_path / "big.tmp"
    target.write_bytes(b"x" * 5000)
    link = root / "alias.tmp"
    link.symlink_to(target)

    found = list(
        excludes.walk_excluded(root, "/bak/x", excludes.parse_patterns(["*.tmp"]))
    )
    assert found == [("/bak/x/alias.tmp", len(str(target)), False)]


def test_walk_prunes_excluded_directory_even_with_negated_child(tmp_path):
    """restic's walker never descends into an excluded dir, so a negated child
    pattern inside it cannot rescue anything. The walk must behave the same."""
    root = tmp_path / "src"
    cache = root / "cache"
    (cache / "keep").mkdir(parents=True)
    (cache / "keep" / "important.txt").write_text("important")
    (cache / "drop.bin").write_text("x" * 10)
    (root / "top.txt").write_text("t")

    found = dict(
        (p, (size, is_dir))
        for p, size, is_dir in excludes.walk_excluded(
            root, "/bak/x", excludes.parse_patterns(["**/cache", "!cache/keep"])
        )
    )
    # One row for the directory, priced by its whole subtree: important.txt (9)
    # + drop.bin (10). The negated child cannot shrink it.
    assert found == {"/bak/x/cache": (19, True)}


def test_collect_from_listing_aggregates_directory_subtree():
    listing = [
        ("/bak/E", 0, True),
        ("/bak/E/cache", 0, True),
        ("/bak/E/cache/a", 100, False),
        ("/bak/E/cache/sub/b", 50, False),
        ("/bak/E/keep", 7, False),
    ]
    result = excludes.collect_from_listing(
        listing, "snap1", excludes.parse_patterns(["cache"])
    )
    assert list(result.entries) == ["/bak/E/cache"]
    assert result.total_size == 150
    assert result.size_in("snap1") == 150
    assert result.entries["/bak/E/cache"].is_dir is True


def test_collect_from_listing_order_does_not_matter():
    """A nested excluded directory must not become its own row, whatever order
    restic happens to list the entries in."""
    patterns = excludes.parse_patterns(["cache"])
    forward = [
        ("/bak/E/cache", 0, True),
        ("/bak/E/cache/sub", 0, True),
        ("/bak/E/cache/sub/b", 50, False),
    ]
    reverse = list(reversed(forward))
    a = excludes.collect_from_listing(forward, "s1", patterns)
    b = excludes.collect_from_listing(reverse, "s1", patterns)
    assert list(a.entries) == list(b.entries) == ["/bak/E/cache"]
    assert a.total_size == b.total_size == 50


def test_collect_from_listing_nested_directories_credit_ancestor():
    """A directory inside an excluded directory is not a separate row.

    restic prunes an excluded directory rather than descending, so `sub` is
    never seen as its own thing — its bytes belong to the directory that
    claimed it. This mirrors walk_excluded exactly.
    """
    listing = [
        ("/bak/E/cache", 0, True),
        ("/bak/E/cache/sub", 0, True),
        ("/bak/E/cache/sub/b", 50, False),
    ]
    result = excludes.collect_from_listing(
        listing, "snap1", excludes.parse_patterns(["cache"])
    )
    assert list(result.entries) == ["/bak/E/cache"]
    assert result.total_size == 50
    assert result.entries["/bak/E/cache"].size == 50


def test_collect_from_listing_tracks_each_snapshot():
    listing = [("/bak/E/a.tmp", 10, False)]
    patterns = excludes.parse_patterns(["*.tmp"])
    result = excludes.collect_from_listing(listing, "s1", patterns)
    result = excludes.collect_from_listing(listing, "s2", patterns, into=result)
    assert result.size_in("s1") == 10
    assert result.size_in("s2") == 10
    assert result.size_in_of("/bak/E/a.tmp", "s1") == 10
    # The bytes are counted once per snapshot, so the total is 20: the same data
    # appears in both. Space actually reclaimed is reported separately, from
    # `restic stats`, which accounts for packing, compression and dedup.
    assert result.total_size == 20


# --- rendering -----------------------------------------------------------


def test_human_bytes():
    assert excludes.human_bytes(0) == "0 B"
    assert excludes.human_bytes(512) == "512 B"
    assert excludes.human_bytes(1024) == "1.0 KiB"
    assert excludes.human_bytes(1536) == "1.5 KiB"
    assert excludes.human_bytes(1024**3) == "1.0 GiB"


def test_human_bytes_edge_cases():
    assert excludes.human_bytes(1024**5) == "1.0 PiB"
    assert excludes.human_bytes(1024**6) == "1024.0 PiB"
    # One decimal above 1 KiB: 1 MiB - 1 B displays as "1.0 MiB".
    assert excludes.human_bytes(1024**2 - 1) == "1.0 MiB"


def test_format_table_totals_cover_everything():
    listing = [
        ("/bak/E/a.tmp", 1024, False),
        ("/bak/E/b.tmp", 2048, False),
        ("/bak/E/keep", 99, False),
    ]
    patterns = excludes.parse_patterns(["*.tmp"])
    result = excludes.collect_from_listing(listing, "s1", patterns)
    result = excludes.collect_from_listing(listing, "s2", patterns, into=result)
    table = excludes.format_table(result, limit=1)
    lines = table.splitlines()
    totals = lines[-2] if lines[-1].startswith("...") else lines[-1]
    assert totals.startswith("BYTES TO EXCLUDE")
    # 1 KiB + 2 KiB per snapshot, then the total. --limit bounds the displayed
    # rows only, never the totals.
    cells = totals[len("BYTES TO EXCLUDE"):].split()
    assert cells == ["3.0", "KiB", "3.0", "KiB", "6.0", "KiB"]
    assert "... 1 more" in table
    assert "b.tmp" not in table


def test_format_table_marks_absent_with_dash():
    """A path missing from a snapshot shows '-', not 0 B, so 'not here' stays
    distinguishable from 'present but empty'."""
    result = excludes.ExclusionSet()
    result.add("s1", "/bak/E/only-s1", 10, False)
    result.add("s2", "/bak/E/only-s2", 20, False)
    lines = excludes.format_table(result).splitlines()
    # Snapshot columns are newest-first; the TOTAL column comes last.
    assert lines[0].split() == ["s2", "s1", "TOTAL"]
    assert "only-s1" in lines[1] and "-" in lines[1]
    assert "only-s2" in lines[2] and "-" in lines[2]


def test_format_table_empty_set_is_still_valid():
    # Nothing excluded and no snapshots: nothing to render. Callers print their
    # own "nothing to remove" line, so an empty string is the honest answer.
    assert excludes.format_table(excludes.ExclusionSet()) == ""


def test_format_entries_limits_with_note():
    result = excludes.ExclusionSet()
    for i in range(5):
        result.add(None, f"/bak/E/f{i}", 1, False)
    out = excludes.format_entries(result, limit=2)
    assert len(out) == 3
    assert out[-1] == "... 3 more"


# --- patterns digest (completion stamp) ---------------------------------


def test_patterns_digest_is_stable_and_order_sensitive():
    a = excludes.patterns_digest(["*.tmp", "cache"])
    assert a == excludes.patterns_digest(["*.tmp", "cache"])
    # Order matters: `!` patterns are evaluated in sequence.
    assert a != excludes.patterns_digest(["cache", "*.tmp"])
    assert a != excludes.patterns_digest(["*.tmp"])
    assert len(a) == 16


def test_patterns_digest_avoids_boundary_collisions():
    # Naive concatenation would collide these two, since "ab\0c" vs "a\0bc".
    assert excludes.patterns_digest(["ab", "c"]) != excludes.patterns_digest(
        ["a", "bc"]
    )


def test_patterns_digest_handles_non_utf8_patterns():
    # Latin-1 bytes are legal in a Linux filename, so a pattern can hold them.
    assert excludes.patterns_digest(["caf\udce9"])

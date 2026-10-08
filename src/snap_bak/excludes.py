"""Restic exclusion patterns: matching, inspection, and retroactive application.

Patterns only govern what a *future* backup skips. Files uploaded before a
rule was added stay in the repo forever, because restic retention prunes whole
snapshots, never a subset of files. This module provides the tools to see what
the current rules actually match (live filesystem, local snapshots, or the
files already stored in restic) and to remove exactly those files from existing
snapshots.

## Matching is a port, not a reimplementation

restic decides exclusions with its own `internal/filter` engine, so any
home-grown glob would eventually disagree with what restic actually does. The
matching below is a faithful port of that engine (restic 0.19.1) plus the Go
`path/filepath` primitives it builds on:

- Go `filepath.Clean` for pattern normalisation (`/foo/../bar` -> `/bar`)
- Go `filepath.Match` per path segment, where `*` never crosses `/` (this is
  deliberately NOT Python's `fnmatch`, whose `*` does cross separators)
- restic's `match`/`childMatch`, including `**` and offset matching, where a
  relative pattern matches at any depth
- restic's `List`/`ListWithChild`, including `!` negation

`tests/test_excludes.py` pins this against restic's own golden cases, and the
integration test cross-checks it against a real restic binary.

## Path basis

Backups run `restic backup <mount_base>/<source basename>`, so the paths restic
stored begin with the *mount point*, not the live source path. Every match in
this module is evaluated against that same mount-point path, which is what
makes "what would the next backup skip?" answerable for the live filesystem and
for local snapshots without mounting anything (mounting would race an in-flight
backup).

## Nothing here modifies the local filesystem

Inspection is read-only. Removing files from backups is restic's job (see
`apply` in cli.py) and never touches the live subvolume or any btrfs snapshot.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

__all__ = [
    "BadPatternError",
    "Pattern",
    "parse_patterns",
    "match",
    "child_match",
    "list_match",
    "excluded",
    "validate_patterns",
    "read_pattern_file",
    "patterns_for_job",
    "lint",
    "ExcludedEntry",
    "ExclusionSet",
    "walk_excluded",
    "parse_ls_long",
    "collect_from_listing",
    "human_bytes",
    "format_table",
    "format_entries",
    "patterns_digest",
]


class BadPatternError(ValueError):
    """A pattern is malformed (Go's filepath.ErrBadPattern equivalent)."""


# --------------------------------------------------------------------------
# Go filepath.Clean / splitPath
# --------------------------------------------------------------------------


def clean_path(p: str) -> str:
    """Port of Go's filepath.Clean for slash-separated paths.

    Lexically removes `.` elements, resolves `..` (which cannot escape a
    leading `/`), collapses repeated separators, and drops any trailing slash —
    so `/foo/bar/` and `/foo//bar` are one path.
    """
    if p == "":
        return "."
    rooted = p.startswith("/")
    out: list[str] = []
    for elem in p.split("/"):
        if elem == "" or elem == ".":
            continue
        if elem == "..":
            if out and out[-1] != "..":
                out.pop()
            elif not rooted:
                # An unrooted path may keep leading ".." elements.
                out.append("..")
            continue
        out.append(elem)
    result = "/".join(out)
    if rooted:
        return "/" + result
    return result or "."


def split_path(p: str) -> list[str]:
    """Split a cleaned path into components; the root becomes the "/" element.

    Mirrors restic's `splitPath`: an absolute path's first component is the
    literal "/", which is how an anchored pattern is told from a relative one
    during matching.
    """
    parts = p.split("/")
    if parts[0] == "":
        parts[0] = "/"
    return parts


# --------------------------------------------------------------------------
# Go filepath.Match, per path segment
# --------------------------------------------------------------------------


def _scan_chunk(pattern: str) -> tuple[bool, str, str]:
    """Split off the leading `*`s and the next literal/class chunk.

    Port of Go's `scanChunk`. A `*` inside a `[...]` class is literal, so the
    scan tracks whether it is inside a class.
    """
    star = False
    while pattern.startswith("*"):
        pattern = pattern[1:]
        star = True
    in_range = False
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            # The backslash escapes the next byte; the syntax check happens in
            # _match_chunk.
            if i + 1 < len(pattern):
                i += 1
        elif c == "[":
            in_range = True
        elif c == "]":
            in_range = False
        elif c == "*":
            if not in_range:
                break
        i += 1
    return star, pattern[:i], pattern[i:]


def _get_esc(chunk: str) -> tuple[str, str]:
    """Read one (possibly escaped) character of a `[...]` class.

    Port of Go's `getEsc`. Raises BadPatternError for the malformed forms Go
    rejects: `-` or `]` with nothing before it, or a trailing lone backslash.
    """
    if not chunk or chunk[0] in "-]":
        raise BadPatternError(f"malformed character class in {chunk!r}")
    if chunk[0] == "\\":
        chunk = chunk[1:]
        if not chunk:
            raise BadPatternError("trailing backslash in character class")
    return chunk[0], chunk[1:]


def _match_chunk(chunk: str, s: str) -> tuple[str, bool]:
    """Match single-character operators at the start of `s`; return the rest.

    Port of Go's `matchChunk`. `chunk` holds literals, `?` and `[...]` classes
    only. `?` never matches `/`. Once a match has failed the remaining pattern
    is still parsed (to keep syntax errors reported) but no longer consumes
    input.
    """
    failed = False
    while chunk:
        if not failed and not s:
            failed = True
        c = chunk[0]
        if c == "[":
            char = ""
            if not failed:
                char, s = s[0], s[1:]
            chunk = chunk[1:]
            negated = False
            if chunk.startswith("^"):
                negated = True
                chunk = chunk[1:]
            matched = False
            ranges = 0
            while True:
                if chunk.startswith("]") and ranges > 0:
                    chunk = chunk[1:]
                    break
                if not chunk:
                    raise BadPatternError("unterminated character class")
                lo, chunk = _get_esc(chunk)
                hi = lo
                if chunk.startswith("-"):
                    hi, chunk = _get_esc(chunk[1:])
                if char and lo <= char <= hi:
                    matched = True
                ranges += 1
            if matched == negated:
                failed = True
        elif c == "?":
            if not failed:
                if s[0] == "/":
                    failed = True
                s = s[1:]
            chunk = chunk[1:]
        elif c == "\\":
            chunk = chunk[1:]
            if not chunk:
                raise BadPatternError("trailing backslash in pattern")
            # fall through to the literal comparison below
            if not failed:
                if chunk[0] != s[0]:
                    failed = True
                s = s[1:]
            chunk = chunk[1:]
        else:
            if not failed:
                if chunk[0] != s[0]:
                    failed = True
                s = s[1:]
            chunk = chunk[1:]
    if failed:
        return "", False
    return s, True


def segment_match(pattern: str, name: str) -> bool:
    """Go `filepath.Match` for a single path segment.

    `*` matches any run of characters within the segment but never a `/`, so
    `*.tmp` cannot match `a/b.tmp` (restic applies this per component).
    """
    while pattern:
        star, chunk, pattern = _scan_chunk(pattern)
        if star and chunk == "":
            # A trailing `*` matches the rest of the segment, but not across a
            # separator.
            return "/" not in name
        rest, ok = _match_chunk(chunk, name)
        if ok and (not rest or pattern):
            name = rest
            continue
        if star:
            # Retry the chunk one byte later; `*` still may not cross a "/".
            for i, ch in enumerate(name):
                if ch == "/":
                    break
                rest, ok = _match_chunk(chunk, name[i + 1 :])
                if ok:
                    if not pattern and rest:
                        continue
                    name = rest
                    break
            else:
                return False
            if not ok:
                return False
            continue
        return False
    return name == ""


# --------------------------------------------------------------------------
# restic's internal/filter
# --------------------------------------------------------------------------


@dataclass
class _Part:
    pattern: str
    is_simple: bool


@dataclass
class Pattern:
    """A preparsed restic exclude pattern."""

    original: str
    parts: list[_Part]
    is_negated: bool = False


def _prepare_pattern(pattern_str: str) -> Pattern:
    """Port of restic's `preparePattern`.

    A leading `!` negates. The pattern is `Clean`ed and split into components;
    `**` becomes the empty string (restic's "matches anything" marker), and a
    part with no glob metacharacters is flagged simple so it can be compared
    with `==` instead of the much slower glob matcher.
    """
    negated = False
    original = pattern_str
    if pattern_str.startswith("!"):
        negated = True
        pattern_str = pattern_str[1:]
    parts = []
    for part in split_path(clean_path(pattern_str)):
        is_simple = not any(ch in part for ch in "\\[]*?")
        parts.append(_Part("" if part == "**" else part, is_simple))
    return Pattern(original, parts, negated)


def parse_patterns(patterns: Iterable[str]) -> list[Pattern]:
    """Port of restic's `ParsePatterns`; empty patterns are dropped."""
    return [_prepare_pattern(p) for p in patterns if p]


def _match_part(part: _Part, segment: str) -> bool:
    if part.is_simple:
        return part.pattern == segment
    return segment_match(part.pattern, segment)


def _has_double_wildcard(p: Pattern) -> tuple[bool, int]:
    for i, part in enumerate(p.parts):
        if part.pattern == "":
            return True, i
    return False, 0


def _match_parts(p: Pattern, strs: list[str]) -> bool:
    """Port of restic's unexported `match`."""
    ok, pos = _has_double_wildcard(p)
    if ok:
        # Expand `**` into a run of `*` parts, one longer per iteration (i=0
        # expands to nothing). restic achieves this by reusing one buffer whose
        # slots stay "*" across iterations; building the run directly is
        # equivalent and avoids re-parsing. With several `**`, the recursive
        # call expands the next one, which is how a pattern absorbs any number
        # of intermediate directories. The bound keeps len(new_parts) <= len(strs).
        max_stars = max(len(strs) - len(p.parts) + 1, 0)
        for i in range(max_stars + 1):
            new_parts = p.parts[:pos] + [_Part("*", False)] * i + p.parts[pos + 1 :]
            if _match_parts(Pattern(p.original, new_parts, p.is_negated), strs):
                return True
        return False

    if not p.parts and not strs:
        return True
    if not p.parts:
        return False

    if len(p.parts) <= len(strs):
        min_offset = 0
        max_offset = len(strs) - len(p.parts)
        if p.parts[0].pattern == "/":
            # Anchored: only the root position can line up.
            max_offset = 0
        elif strs[0] == "/":
            # An unrooted pattern may align anywhere below the root marker.
            min_offset = 1
        for offset in range(max_offset, min_offset - 1, -1):
            if all(
                _match_part(p.parts[i], strs[offset + i])
                for i in range(len(p.parts) - 1, -1, -1)
            ):
                return True
    return False


def _child_match_parts(p: Pattern, strs: list[str]) -> bool:
    """Port of restic's unexported `childMatch` (could a child still match?)."""
    if not p.parts or p.parts[0].pattern != "/":
        # A relative pattern can always match further down.
        return True
    ok, pos = _has_double_wildcard(p)
    if ok and len(strs) >= pos:
        strs = strs[:pos]
    length = min(len(strs), len(p.parts))
    return _match_parts(Pattern(p.original, p.parts[:length], p.is_negated), strs)


def match(pattern_str: str, path: str) -> bool:
    """Does `path` match `pattern_str`? (restic's `filter.Match`)

    A `!` prefix is not honoured here (restic's Match rejects it too); use
    list_match for a whole pattern set.
    """
    if pattern_str == "":
        return True
    if path == "":
        raise BadPatternError("filter.Match: path is empty")
    return _match_parts(_prepare_pattern(pattern_str), split_path(path))


def child_match(pattern_str: str, path: str) -> bool:
    """Could a child of `path` match `pattern_str`? (restic's `ChildMatch`)"""
    if pattern_str == "":
        return True
    if path == "":
        raise BadPatternError("filter.ChildMatch: path is empty")
    return _child_match_parts(_prepare_pattern(pattern_str), split_path(path))


def list_match(patterns: Sequence[Pattern], path: str) -> tuple[bool, bool]:
    """Port of restic's `ListWithChild`: (is_excluded, child_may_match).

    A negated (`!`) pattern subtracts from the accumulated result, so
    `["**", "!keep"]` excludes everything except `keep`.
    """
    if not patterns:
        return False, False
    if path == "":
        raise BadPatternError("filter.List: path is empty")
    strs = split_path(path)
    has_negated = any(p.is_negated for p in patterns)

    matched = False
    child_may_match = False
    for p in patterns:
        m = _match_parts(p, strs)
        c = _child_match_parts(p, strs)
        if p.is_negated:
            matched = matched and not m
            child_may_match = child_may_match and not m
        else:
            matched = matched or m
            child_may_match = child_may_match or c
            if matched and child_may_match and not has_negated:
                # Without negation nothing later can change the answer.
                break
    return matched, child_may_match


def excluded(patterns: Sequence[Pattern], path: str) -> bool:
    """True when `path` is excluded by `patterns` (restic's `RejectByPattern`).

    Raises BadPatternError for a malformed pattern — what restic would reject —
    rather than silently answering False.
    """
    matched, _ = list_match(patterns, path)
    return matched


def validate_patterns(patterns: Iterable[str]) -> list[str]:
    """Port of restic's `ValidatePatterns`: the patterns restic would reject.

    restic validates each pattern by matching it against itself, so a malformed
    glob (`test/[`) is caught before a backup runs rather than mid-backup.
    """
    invalid = []
    for p in parse_patterns(patterns):
        for part in p.parts:
            try:
                segment_match(part.pattern, part.pattern)
            except BadPatternError:
                invalid.append(p.original)
                break
    return invalid


# --------------------------------------------------------------------------
# Pattern sources: config `excludes` + `ignore_file`
# --------------------------------------------------------------------------


def read_pattern_file(path: str | os.PathLike) -> list[str]:
    """Port of restic's `readPatternsFromFiles`.

    Each line is trimmed; blanks and `#` comments are dropped; `$VAR` expands
    from the environment and `$$` yields a literal `$` (restic's `os.Expand`
    with `getenvOrDollar`).
    """
    text = Path(path).read_text()
    patterns: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(_expand_env(line))
    return patterns


def _expand_env(line: str) -> str:
    """Expand `$VAR`/`${VAR}`; `$$` is a literal `$` (restic's semantics)."""
    out: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch != "$":
            out.append(ch)
            i += 1
            continue
        if i + 1 < len(line) and line[i + 1] == "$":
            out.append("$")
            i += 2
            continue
        j = i + 1
        braced = j < len(line) and line[j] == "{"
        if braced:
            j += 1
        name_start = j
        while j < len(line) and (line[j].isalnum() or line[j] == "_"):
            j += 1
        name = line[name_start:j]
        if braced:
            if j < len(line) and line[j] == "}":
                j += 1
            else:
                # Unterminated ${ — treat the `$` literally.
                out.append(ch)
                i += 1
                continue
        if not name:
            out.append(ch)
            i += 1
            continue
        out.append(os.environ.get(name, ""))
        i = j
    return "".join(out)


def patterns_for_job(job) -> list[str]:
    """The merged pattern list for a job: config `excludes` + `ignore_file`.

    Order matches what restic receives (`--exclude-file` then `--exclude`), and
    matters for `!` negation.
    """
    patterns: list[str] = []
    if job.ignore_file:
        patterns.extend(read_pattern_file(job.ignore_file))
    patterns.extend(job.excludes)
    return patterns


# --------------------------------------------------------------------------
# Lint: patterns that are broken, or that can never match
# --------------------------------------------------------------------------


@dataclass
class LintIssue:
    """One problem with a pattern, found without touching the filesystem."""

    pattern: str
    kind: str  # "invalid" | "outside_root"
    message: str


def lint(patterns: Sequence[str], backup_root: str) -> list[LintIssue]:
    """Report patterns that are malformed, or can never match `backup_root`.

    The commonest authoring mistake is an anchored pattern written against the
    *live* path (`/home/user/.cache`) when restic actually saw the mount point
    (`/bak/home/.cache`). Such a pattern silently excludes nothing, which is
    worth saying out loud rather than letting it fail quietly forever.
    """
    issues: list[LintIssue] = []
    for pattern in validate_patterns(patterns):
        issues.append(
            LintIssue(pattern, "invalid", "malformed pattern; restic rejects this")
        )
    root = clean_path(backup_root)
    for pattern in patterns:
        if pattern.startswith("!"):
            # A negation can only re-include, never exclude, so "can never
            # match" does not apply to it. Malformed ones are still caught by
            # validate_patterns above.
            continue
        body = pattern
        if not body.startswith("/"):
            continue  # a relative pattern matches at any depth
        if "**" in body:
            continue  # `**` can reach anything below the root
        normalised = clean_path(body)
        if normalised == root or normalised.startswith(root + "/"):
            continue
        issues.append(
            LintIssue(
                pattern,
                "outside_root",
                f"anchored at {normalised!r}, which is outside the backup root "
                f"{root!r}; it can never match (did you mean the mount point?)",
            )
        )
    return issues


# --------------------------------------------------------------------------
# Exclusion sets: rows for the report, aggregated per snapshot
# --------------------------------------------------------------------------


@dataclass
class ExcludedEntry:
    """One excluded thing: a matched file, or a matched directory + subtree."""

    path: str
    is_dir: bool
    size: int = 0

    @property
    def sort_key(self) -> tuple:
        return (not self.is_dir, self.path)


@dataclass
class ExclusionSet:
    """Excluded entries for one job, with per-snapshot byte totals."""

    entries: dict[str, ExcludedEntry] = field(default_factory=dict)
    # snapshot label -> {entry path: bytes}
    per_snapshot: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, snapshot: str | None, path: str, size: int, is_dir: bool) -> None:
        entry = self.entries.get(path)
        if entry is None:
            entry = self.entries[path] = ExcludedEntry(path, is_dir)
        elif is_dir:
            entry.is_dir = True
        entry.size += size
        if snapshot is not None:
            self.per_snapshot.setdefault(snapshot, {})
            self.per_snapshot[snapshot][path] = (
                self.per_snapshot[snapshot].get(path, 0) + size
            )

    def contains(self, path: str) -> bool:
        return path in self.entries

    def size_in(self, snapshot: str) -> int:
        """Total bytes excluded from one snapshot."""
        return sum(self.per_snapshot.get(snapshot, {}).values())

    def size_in_of(self, path: str, snapshot: str) -> int:
        """Bytes one entry contributes to one snapshot."""
        return self.per_snapshot.get(snapshot, {}).get(path, 0)

    @property
    def total_size(self) -> int:
        return sum(e.size for e in self.entries.values())

    def ordered_entries(self) -> list[ExcludedEntry]:
        return sorted(self.entries.values(), key=lambda e: e.sort_key)


def _subtree_size(root: Path) -> int:
    """Total apparent size of every file beneath `root` (symlinks not followed).

    Used to price a matched directory as one row. Symlinks are counted by their
    own size and never traversed, matching how the walk classifies them.
    """
    total = 0
    for dirpath, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def _claiming_directory(matched_dirs: Sequence[str], path: str) -> str | None:
    """The shallowest matched directory containing `path`, if any.

    A matched directory excludes its whole subtree, so everything beneath it is
    accounted for by that directory's row instead of being listed separately.
    The *shallowest* claimant wins, which is what restic's walker does: it
    prunes an excluded directory rather than descending into it, so a nested
    excluded directory never becomes a row of its own.
    """
    best = None
    for d in matched_dirs:
        if path.startswith(d + "/") and (best is None or len(d) < len(best)):
            best = d
    return best


# --------------------------------------------------------------------------
# Source 1: local filesystem walks (live source + btrfs snapshot dirs)
# --------------------------------------------------------------------------


def walk_excluded(
    root: Path,
    backup_root: str,
    patterns: Sequence[Pattern],
) -> Iterator[tuple[str, int, bool]]:
    """Yield (backup-time path, size, is_dir) for everything excluded under `root`.

    `backup_root` is the mount-point path restic saw at backup time (e.g.
    `/bak/data`); every candidate is rewritten onto it, so results describe
    what a backup would exclude rather than what matches some path string.

    A matched directory is yielded once with the summed size of its subtree,
    and its contents are not yielded individually — matching restic, which
    prunes an excluded directory rather than descending into it. Symlinks are
    never followed: restic stores a link as a link.

    Unreadable directories and vanished files are skipped, not fatal — an
    inspection must survive a source subvolume it cannot read.
    """
    root = Path(root)
    base = clean_path(backup_root)

    def scan(directory: Path, rel: str) -> Iterator[tuple[str, int, bool]]:
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: e.name)
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            return
        for entry in entries:
            child_rel = f"{rel}/{entry.name}" if rel else entry.name
            child_path = f"{base}/{child_rel}"
            try:
                # follow_symlinks=False: a symlinked directory is stored as a
                # link, so it is reported by its own name, never descended into.
                is_dir = entry.is_dir(follow_symlinks=False)
                hit = excluded(patterns, child_path)
            except (OSError, BadPatternError):
                # OSError: the entry vanished or is unreadable.
                # BadPatternError: restic would reject the pattern outright,
                # which --lint reports; here the path is simply skipped.
                continue
            if is_dir:
                if hit:
                    # One row for the directory, bytes = whole subtree.
                    yield child_path, _subtree_size(Path(entry.path)), True
                    continue
                yield from scan(Path(entry.path), child_rel)
            elif hit:
                # Priced by its own length, so a symlink contributes only the
                # link: its target's bytes are not in the snapshot.
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                yield child_path, size, False

    yield from scan(root, "")


# --------------------------------------------------------------------------
# Source 2: files already stored in restic (`restic ls --long`)
# --------------------------------------------------------------------------
# Six whitespace-separated fields (mode uid gid size date time), then the path,
# which may hold spaces and unicode. Directories are identified by a mode
# starting with 'd' (restic appends no trailing slash, even in plain `ls`).
# The leading "snapshot <id> of [...]" header line is skipped.


def parse_ls_long(text: str) -> Iterator[tuple[str, int, bool]]:
    """Parse `restic ls --long` output into (path, size, is_dir).

    The path is the remainder after exactly six fields, never a whitespace
    split, so names with spaces survive. Lines whose size field is not an
    integer are skipped rather than guessed at.
    """
    for line in text.splitlines():
        if not line.strip() or line.startswith("snapshot "):
            continue
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        mode, _uid, _gid, size_s, _date, _time, path = parts
        try:
            size = int(size_s)
        except ValueError:
            continue
        yield path, size, mode.startswith("d")


def collect_from_listing(
    paths: Iterable[tuple[str, int, bool]],
    snapshot: str,
    patterns: Sequence[Pattern],
    into: ExclusionSet | None = None,
) -> ExclusionSet:
    """Filter a stored file listing through `patterns`, accumulating an ExclusionSet.

    Excluded directories collapse into a single row carrying their subtree's
    bytes, mirroring the walk (restic prunes rather than descending). `snapshot`
    labels the per-snapshot column.

    Two passes, so the result does not depend on listing order: first find the
    directories that match (these prune their subtrees), then credit each path
    to the shallowest directory claiming it, or make it a row of its own.
    """
    result = into if into is not None else ExclusionSet()
    items = list(paths)

    matched_dirs: list[str] = []
    for path, _size, is_dir in items:
        if not is_dir:
            continue
        try:
            if excluded(patterns, path):
                matched_dirs.append(path)
        except BadPatternError:
            # restic rejects the pattern; --lint reports that separately.
            continue

    for path, size, is_dir in items:
        parent = _claiming_directory(matched_dirs, path)
        if parent is not None:
            # Inside an excluded directory: the directory's row carries it.
            result.add(snapshot, parent, size, False)
            continue
        try:
            hit = excluded(patterns, path)
        except BadPatternError:
            continue
        if hit:
            result.add(snapshot, path, size, is_dir)
    return result


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def human_bytes(n: int) -> str:
    """A byte count in binary units: `512 B`, `1.5 KiB`, `1.0 GiB`.

    1024-based, like the size units config.json accepts. One decimal place above
    1 KiB and whole bytes below it. A value that rounds up to a unit boundary
    (1023.999 KiB) is promoted rather than printed as `1024.0 KiB`.
    """
    value = float(n)
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    idx = 0
    while abs(value) >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    if idx == 0:
        return f"{int(value)} B"
    value = round(value, 1)
    if value >= 1024 and idx < len(units) - 1:
        value = round(value / 1024, 1)
        idx += 1
    return f"{value:.1f} {units[idx]}"


ABSENT = "-"
_TOTALS_LABEL = "BYTES TO EXCLUDE"


def _cell(path: str, snapshot: str, exclusions: ExclusionSet) -> str:
    """Bytes this entry drops from `snapshot`, or '-' when absent from it.

    '-' rather than `0 B` keeps "not present here" distinguishable from
    "present and empty".
    """
    if path not in exclusions.per_snapshot.get(snapshot, {}):
        return ABSENT
    return human_bytes(exclusions.size_in_of(path, snapshot))


def format_table(exclusions: ExclusionSet, limit: int | None = None) -> str:
    """The per-snapshot table: one row per excluded path, plus a totals row.

    One column per snapshot (newest first). `limit` bounds the rows shown, but
    the totals always cover everything, so truncation never understates it.
    """
    snapshots = sorted(exclusions.per_snapshot, reverse=True)
    entries = exclusions.ordered_entries()
    shown = entries[:limit] if limit else entries
    label_width = max([len(_TOTALS_LABEL)] + [len(e.path) for e in shown])

    widths = [max(9, len(s)) for s in snapshots] + [12]
    # With no snapshots the header is blank rather than absent, so the layout
    # stays obvious when a selector produced nothing.
    header = ["".ljust(label_width)] + [
        s.rjust(w) for s, w in zip(snapshots, widths)
    ]
    if snapshots:
        header.append("TOTAL".rjust(widths[-1]))
    lines = ["  ".join(header).rstrip()]
    for entry in shown:
        lines.append(
            "  ".join(
                [entry.path.ljust(label_width)]
                + [
                    _cell(entry.path, s, exclusions).rjust(w)
                    for s, w in zip(snapshots, widths)
                ]
                + ([human_bytes(entry.size).rjust(widths[-1])] if snapshots else [])
            ).rstrip()
        )
    if snapshots:
        lines.append(
            "  ".join(
                # The totals row is not an excluded path — it aggregates them.
                [_TOTALS_LABEL.ljust(label_width)]
                + [
                    human_bytes(exclusions.size_in(s)).rjust(w)
                    for s, w in zip(snapshots, widths)
                ]
                + [human_bytes(exclusions.total_size).rjust(widths[-1])]
            ).rstrip()
        )
    if limit and len(entries) > limit:
        lines.append(f"... {len(entries) - limit} more (totals cover all)")
    return "\n".join(lines)


def format_entries(exclusions: ExclusionSet, limit: int | None = None) -> list[str]:
    """Excluded paths as a plain list (the `--live` presentation).

    `limit` bounds the list, with a trailing note for what was withheld.
    """
    entries = exclusions.ordered_entries()
    shown = entries[:limit] if limit else entries
    out = [e.path for e in shown]
    if limit and len(entries) > limit:
        out.append(f"... {len(entries) - limit} more")
    return out


def patterns_digest(patterns: Sequence[str]) -> str:
    """A short stable digest of a pattern list.

    The completion stamp stores this, so a change to the rules invalidates a
    previous "already applied" mark. Patterns are NUL-joined before hashing, so
    `["ab", "c"]` and `["a", "bc"]` cannot collide. Non-UTF-8 bytes in a pattern
    (possible on Linux) survive via surrogateescape.
    """
    joined = "\0".join(patterns)
    return hashlib.sha256(joined.encode("utf-8", "surrogateescape")).hexdigest()[:16]

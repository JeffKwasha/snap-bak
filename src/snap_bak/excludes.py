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

import functools
import hashlib
import os
import stat
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "BadPatternError",
    "ExcludedEntry",
    "ExclusionSet",
    "Pattern",
    "child_match",
    "collect_from_listing",
    "excluded",
    "lint",
    "list_match",
    "match",
    "parse_ls_long",
    "parse_patterns",
    "patterns_digest",
    "patterns_for_job",
    "read_pattern_file",
    "validate_patterns",
    "walk_excluded",
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
        elif c == "*" and not in_range:
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


@functools.lru_cache(maxsize=1 << 17)
def segment_match(pattern: str, name: str) -> bool:
    """Go `filepath.Match` for a single path segment.

    `*` matches any run of characters within the segment but never a `/`, so
    `*.tmp` cannot match `a/b.tmp` (restic applies this per component).

    Memoised: the same (pattern, segment) pairs recur constantly — `share`,
    `lib`, `.git`, file extensions — and the matcher is pure, so caching cannot
    change an answer, only repeat one. This is the difference between the walk
    spending its time in matching and spending it in the kernel.
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


@dataclass(frozen=True)
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


# Resolving a pattern's `**` into concrete part lists costs real time and is
# repeated for every path, so it is memoised. Resolving the *match* is not
# memoised, and must not be: matching depends on the segment values, so
# `*.tmp` matches a 4-segment `/bak/E/a.tmp` but not `/bak/E/b.txt` — the same
# pattern and the same segment count, different answers. Only the shape of a
# pattern is independent of the path, so only the shape is cached.
_RESOLVED_CACHE: dict[tuple, tuple[Pattern, ...]] = {}
_RESOLVED_LIMIT = 20_000


def clear_matcher_cache() -> None:
    """Drop memoised pattern expansions. Call between independent scans.

    The segment cache holds pure answers and never goes stale, so clearing it
    is memory hygiene between scans, not correctness.
    """
    _RESOLVED_CACHE.clear()
    segment_match.cache_clear()


def _resolve(p: Pattern, segment_count: int) -> tuple[Pattern, ...]:
    """Every part-list `p` can take for a path of `segment_count` segments.

    `**` becomes a run of `*` parts, one longer per iteration (i=0 expands to
    nothing). restic reaches the same set by reusing one buffer whose slots stay
    "*" across iterations; building each run directly is equivalent and avoids
    re-parsing. Several `**` recurse, which is how a pattern absorbs any number
    of intermediate directories — so `**/x/**` expands into a family that is
    resolved here once rather than re-expanded on every path that reaches it.

    Each round derives its own bound from the pattern *as it stands at that
    round*. That is not incidental: expanding a later `**` against the original
    segment count instead silently drops anchorings, so `/a/**/b/**/c` never
    matches `/a/x/b/y/c` because the run that would absorb the single `x` is
    never generated. restic's own golden cases pin this.
    """
    # Keyed on the parts themselves, not on (original, part count): mid-expansion
    # children of the same pattern share both of those while being entirely
    # different patterns — `/a/**/b/**/c` at pos 2 and pos 4 are six parts of the
    # same original text. Keying on them returns one child's shapes for the
    # other, and `/a/**/b/**/c` then fails to match `/a/x/b/y/c`.
    key = (
        p.original,
        p.is_negated,
        segment_count,
        tuple((pt.pattern, pt.is_simple) for pt in p.parts),
    )
    hit = _RESOLVED_CACHE.get(key)
    if hit is not None:
        return hit

    ok, pos = _has_double_wildcard(p)
    if not ok:
        out = (p,)
    else:
        max_stars = max(segment_count - len(p.parts) + 1, 0)
        seen: list[Pattern] = []
        for i in range(max_stars + 1):
            candidate = Pattern(
                p.original,
                p.parts[:pos] + [_Part("*", False)] * i + p.parts[pos + 1 :],
                p.is_negated,
            )
            # `len(strs) - len(candidate.parts)` is the bound the recursive
            # call in `_match_parts` would compute for itself, so recursing with
            # `segment_count` reproduces the original recursion exactly rather
            # than freezing the count at the first round.
            seen.extend(_resolve(candidate, segment_count))
        # Deduplicate on the part list: `Pattern` holds a list and is not
        # hashable, and several expansions of the same `**` coincide.
        out = tuple(
            {tuple((pt.pattern, pt.is_simple) for pt in q.parts): q for q in seen}.values()
        )

    if len(_RESOLVED_CACHE) < _RESOLVED_LIMIT:
        _RESOLVED_CACHE[key] = out
    return out


def _match_parts(p: Pattern, strs: list[str]) -> bool:
    """Port of restic's unexported `match`."""
    if _has_double_wildcard(p)[0]:
        for resolved in _resolve(p, len(strs)):
            if _match_parts(resolved, strs):
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


def list_match(patterns: Sequence[Pattern], path: str) -> tuple[bool, str | None]:
    """Port of restic's `ListWithChild`: (is_excluded, matched_rule).

    A negated (`!`) pattern subtracts from the accumulated result, so
    `["**", "!keep"]` excludes everything except `keep`. The second element is
    the first non-negated pattern that matched, for attributing a row to a rule.
    Stops at the first match — one is enough.
    """
    if not patterns:
        return False, None
    if path == "":
        raise BadPatternError("filter.List: path is empty")
    strs = split_path(path)
    has_negated = any(p.is_negated for p in patterns)

    matched = False
    rule = None
    for p in patterns:
        m = _match_parts(p, strs)
        if p.is_negated:
            if m:
                matched = False
                rule = None
        else:
            if m:
                matched = True
                rule = p.original
                if not has_negated:
                    break
    return matched, rule


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
    # The pattern text responsible, when attribution was requested. A directory
    # row is attributed to the rule that matched the directory itself, not to
    # whatever happened to match the files inside it.
    rule: str | None = None
    # Files this row accounts for: 1 for a file, the subtree's count for a
    # matched directory. Kept on the row so a per-row CSV can carry it, rather
    # than only having the set-wide total. Directories are never counted, so a
    # matched empty directory reports 0.
    files: int = 0
    # A matched directory whose subtree was not read (shallow walk) reports no
    # counts at all rather than a misleading 0. `partial` marks a price that
    # counts only part of its subtree: entries unread or vanished mid-walk, or,
    # beneath a revealed directory, direct children the config excludes anyway.
    unknown: bool = False
    partial: bool = False

    @property
    def sort_key(self) -> tuple:
        return (not self.is_dir, self.path)


@dataclass
class RuleTotals:
    """What one exclude rule accounts for: how much, and under which rule."""

    rule: str
    files: int = 0
    bytes: int = 0
    # A rule (in one source) is `unknown` when every row behind it is unpriced,
    # and `partial` when a price exists but does not cover everything the rule
    # matched. Rendered as `?` and `+?` respectively.
    unknown: bool = False
    partial: bool = False

    def merge(self, other: RuleTotals) -> None:
        """Fold a second source's totals into these.

        Mixed known/unknown counts can only ever be `partial`: the question
        `?` answers is whether the rule's impact is priced at all in this
        source.
        """
        self.files += other.files
        self.bytes += other.bytes
        saw_unknown = self.unknown or other.unknown
        saw_partial = self.partial or other.partial
        if self.files or self.bytes:
            self.unknown = False
            self.partial = saw_unknown or saw_partial
        else:
            self.unknown = saw_unknown
            self.partial = saw_partial

    @property
    def rough(self) -> str | None:
        """`?` when unpriced, `+?` when partly priced; None with no caveat."""
        if self.unknown:
            return "?"
        if self.partial:
            return "+?"
        return None

    def to_json(self) -> dict:
        out = {"rule": self.rule, "files": self.files, "bytes": self.bytes}
        rough = self.rough
        if rough is not None:
            out["rough"] = rough
        return out


# A row restic excluded without any single non-negated rule naming it. Should not
# happen — `excluded()` returning true implies one matched — so it is a row worth
# noticing rather than one to hide, and it gets its own line in the report.
UNATTRIBUTED = "(unattributed)"

# The live source subvolume, named so it can sit in the same per-source map as
# every snapshot rather than needing a special case at each reader.
LIVE = "live"


@dataclass
class ExclusionSet:
    """Excluded entries for one job, with per-snapshot file and byte totals."""

    entries: dict[str, ExcludedEntry] = field(default_factory=dict)
    # snapshot label -> {entry path: bytes}
    per_snapshot: dict[str, dict[str, int]] = field(default_factory=dict)
    # snapshot label -> {entry path: files}. Kept beside the bytes because the
    # report shows both per source, and a bytes-only map cannot answer "how many
    # files does this rule drop here" for one snapshot without re-walking.
    per_snapshot_files: dict[str, dict[str, int]] = field(default_factory=dict)
    # Whether `add` should attribute each row to the rule that matched it.
    # Off by default because it costs a match per entry, and callers that only
    # render a flat list do not need it.
    attribute_rules: bool = False

    def add(
        self,
        snapshot: str | None,
        path: str,
        size: int,
        is_dir: bool,
        rule: str | None = None,
        files: int = 0,
        unknown: bool = False,
        partial: bool = False,
    ) -> None:
        """Record one excluded path.

        `files` is how many this row accounts for: 1 for a file, the subtree's
        count for a matched directory. A directory is never itself a file, so a
        matched empty one reports 0. An `unknown` row carries no counts (its
        subtree was not read); a `partial` row's counts cover its subtree only
        in part.
        """
        entry = self.entries.get(path)
        if entry is None:
            entry = self.entries[path] = ExcludedEntry(
                path, is_dir, rule=rule, files=0
            )
        elif is_dir:
            entry.is_dir = True
        elif entry.rule is None:
            entry.rule = rule
        entry.size += size
        entry.files += files
        entry.unknown = entry.unknown or unknown
        entry.partial = entry.partial or partial
        if snapshot is not None:
            bucket = self.per_snapshot.setdefault(snapshot, {})
            bucket[path] = bucket.get(path, 0) + size
            files_bucket = self.per_snapshot_files.setdefault(snapshot, {})
            files_bucket[path] = files_bucket.get(path, 0) + files

    def contains(self, path: str) -> bool:
        return path in self.entries

    def size_in(self, snapshot: str) -> int:
        """Total bytes excluded from one snapshot."""
        return sum(self.per_snapshot.get(snapshot, {}).values())

    def size_in_of(self, path: str, snapshot: str) -> int:
        """Bytes one entry contributes to one snapshot."""
        return self.per_snapshot.get(snapshot, {}).get(path, 0)

    def files_in(self, snapshot: str) -> int:
        """Total files excluded from one snapshot."""
        return sum(self.per_snapshot_files.get(snapshot, {}).values())

    def files_in_of(self, path: str, snapshot: str) -> int:
        """Files one entry contributes to one snapshot."""
        return self.per_snapshot_files.get(snapshot, {}).get(path, 0)

    @property
    def total_size(self) -> int:
        return sum(e.size for e in self.entries.values())

    @property
    def total_files(self) -> int:
        """Files that would be dropped.

        Free: the traversal producing byte totals already visited every file it
        counted. Not `len(entries)` — one directory row stands for a subtree.
        """
        return sum(
            sum(files.values()) for files in self.per_snapshot_files.values()
        )

    def by_rule(self) -> list[RuleTotals]:
        """Totals per exclude rule, largest first, unattributed rows last.

        A row with no rule (attribution was off, or only a negation decided the
        outcome) is grouped under `None` rather than being silently dropped —
        bytes that cannot be explained are the ones worth noticing.
        """
        totals: dict[str | None, RuleTotals] = {}
        for entry in self.entries.values():
            t = totals.get(entry.rule)
            if t is None:
                t = totals[entry.rule] = RuleTotals(entry.rule or UNATTRIBUTED)
            t.merge(
                RuleTotals(
                    t.rule, files=entry.files, bytes=entry.size,
                    unknown=entry.unknown, partial=entry.partial,
                )
            )
        return sorted(totals.values(), key=lambda t: (-t.bytes, t.rule))

    def by_rule_per_source(
        self, sources: Sequence[str]
    ) -> dict[str, dict[str, RuleTotals]]:
        """`rule -> source -> totals`, over the sources named.

        Joined here rather than in the renderer, which would otherwise repeat
        this loop once per output format. Absent cells mean zero, not unscanned.
        """
        grid: dict[str, dict[str, RuleTotals]] = {}
        for rule, source, path in self.rule_source_cells(sources):
            label = rule or UNATTRIBUTED
            cell = grid.setdefault(label, {}).setdefault(
                source, RuleTotals(label)
            )
            entry = self.entries[path]
            cell.merge(
                RuleTotals(
                    label,
                    files=self.files_in_of(path, source),
                    bytes=self.size_in_of(path, source),
                    unknown=entry.unknown,
                    partial=entry.partial,
                )
            )
        return grid

    def rule_source_cells(
        self, sources: Sequence[str]
    ) -> Iterator[tuple[str | None, str, str]]:
        """`(rule, source, path)` for every (row, source) among `sources`.

        Restricted to the sources named, because a caller building one column at
        a time must not fold every snapshot's totals into each of them. Only
        non-zero cells are yielded — or cells an unpriced row stands behind, so
        an unknown-anything cell is explained rather than silently blank — and
        the renderer never has to ask whether a blank is "scanned and found
        nothing" or "not scanned".
        """
        for entry in self.entries.values():
            for source in sources:
                if (
                    self.files_in_of(entry.path, source)
                    or self.size_in_of(entry.path, source)
                    or entry.unknown
                    or entry.partial
                ):
                    yield entry.rule, source, entry.path

    @property
    def has_rough(self) -> bool:
        """True when any row's counts are unknown or only partly priced."""
        return any(
            e.unknown or e.partial for e in self.entries.values()
        )

    def ordered_entries(self) -> list[ExcludedEntry]:
        return sorted(self.entries.values(), key=lambda e: e.sort_key)


def _node_size(path: str, is_symlink: bool) -> int:
    """Bytes restic would hold for one stored node.

    A symlink costs 0: `restic ls --long` reports it at 0, so charging it
    `lstat`'s size (the length of the target path) would make the live walk
    report bytes the repository does not hold. Hardlinks are not special-cased
    because restic's `ls` does not special-case them either, so hardlinked data
    is over-counted by exactly the shared blocks.
    """
    if is_symlink:
        return 0
    try:
        return os.lstat(path).st_size
    except OSError:
        return 0


def _subtree_files(
    root: Path, skipped: list | None = None
) -> Iterator[tuple[Path, int]]:
    """Yield (path, size) for every file beneath `root`. Symlinks are not followed.

    A symlink — including one that points at a directory — counts as one file
    worth 0 bytes, because that is the node restic stores, and `os.walk` files a
    symlinked directory under `_dirs` rather than `_names`.

    An entry that could not be stat'd is skipped either way, and so is a
    directory that could not be read at all (`os.walk` would otherwise swallow
    the descent silently); when `skipped` is given, each such loss is appended
    to it, so the caller can price the row as `partial` instead of silently
    undercounting it.
    """
    onerror = (
        (lambda err: skipped.append(err.filename))
        if skipped is not None else None
    )
    for dirpath, dirs, names in os.walk(root, followlinks=False, onerror=onerror):
        for name in names:
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
                if stat.S_ISLNK(st.st_mode):
                    yield Path(full), 0
                else:
                    yield Path(full), st.st_size
            except OSError:
                if skipped is not None:
                    skipped.append(full)
                continue
        for name in [d for d in dirs if os.path.islink(os.path.join(dirpath, d))]:
            yield Path(os.path.join(dirpath, name)), 0


def _walk_chunk(root_path: str) -> tuple[int, int, int]:
    """Price one subtree: (file count, total bytes, unreadable entries)."""
    total = 0
    count = 0
    skipped: list = []
    for _fpath, fsize in _subtree_files(Path(root_path), skipped):
        total += fsize
        count += 1
    return count, total, len(skipped)


def _walk_chunk_expanded(
    root_path: str,
) -> tuple[list[str], list[int], int]:
    paths = []
    sizes = []
    skipped: list = []
    for fpath, fsize in _subtree_files(Path(root_path), skipped):
        paths.append(str(fpath))
        sizes.append(fsize)
    return paths, sizes, len(skipped)


_worker_patterns: list[Pattern] | None = None
_worker_reveal: list[Pattern] = []

# Subtrees dispatched before any result is drained, so a source with thousands
# of directories does not queue them all at once.
_MAX_OUTSTANDING = 32


def _init_worker(
    patterns: Sequence[Pattern], reveal: Sequence[Pattern] = ()
) -> None:
    global _worker_patterns, _worker_reveal
    _worker_patterns = list(patterns)
    _worker_reveal = list(reveal)


def _scan_subtree(args: tuple) -> list[tuple]:
    root_path, backup_root, attribute_rules, expand, skip, shallow = args
    assert _worker_patterns is not None
    return [
        (w.path, w.size, w.is_dir, w.rule, w.files, w.detail, w.unknown, w.partial)
        for w in walk_excluded(
            Path(root_path), backup_root, _worker_patterns,
            attribute_rules=attribute_rules, expand=expand, skip=skip,
            reveal=_worker_reveal, shallow=shallow,
        )
    ]


def _subtree_size(root: Path) -> int:
    """Total apparent size of every file beneath `root` (symlinks not followed).

    Kept as a named single-purpose wrapper: several callers want only the bytes.
    """
    return sum(size for _path, size in _subtree_files(root))


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


@dataclass
class WalkedPath:
    """One excluded path found by a filesystem walk.

    A record rather than a bare tuple because the walk now also reports which
    rule matched, how many files the row stands for, and whether this is an
    expanded per-file row or a rule-level one — and unpacking a growing tuple at
    three call sites was getting unreadable.
    """

    path: str
    size: int
    is_dir: bool
    rule: str | None = None
    # Files this row accounts for. Always >= 1, so `None` (unknown) is
    # distinguishable from a genuinely empty directory.
    files: int = 1
    # True for a file yielded from inside a matched directory when the caller
    # asked to see inside one. Those rows belong to the verbose report only: the
    # rule table already counts the subtree through the directory's own row, so
    # adding them there would count every file twice.
    detail: bool = False
    # A matched directory the walk did not descend into: `unknown` rows carry no
    # counts, because restic prunes an excluded directory without reading it, and
    # the report does not pay I/O restic would not pay. `partial` marks a
    # directory whose price is incomplete: entries that vanished mid-walk or
    # could not be read, and — beneath a revealed directory — direct children
    # the configuration already excludes (those get their own unpriced rows,
    # because the point of a reveal pattern is what it adds to the config).
    unknown: bool = False
    partial: bool = False


def walk_excluded(
    root: Path,
    backup_root: str,
    patterns: Sequence[Pattern],
    attribute_rules: bool = False,
    progress=None,
    expand: bool = False,
    pool=None,
    skip: str | None = None,
    top_level: bool = False,
    shallow: bool = False,
    reveal: Sequence[Pattern] = (),
) -> Iterator[WalkedPath]:
    """Yield every excluded path under `root`, with its size and rule.

    `backup_root` is the mount-point path restic saw at backup time (e.g.
    `/bak/data`); every candidate is rewritten onto it, so results describe
    what a backup would exclude rather than what matches some path string.

    A matched directory is yielded once with the summed size of its subtree,
    and its contents are not yielded individually — matching restic, which
    prunes an excluded directory rather than descending into it. Symlinks are
    never followed: restic stores a link as a link.

    `expand` additionally yields each file inside a matched directory, marked
    `detail=True` and attributed to the rule that matched the *directory*. That
    attribution is deliberate: restic pruned the directory, so no pattern ever
    reached those files, and crediting them to a rule that would have matched
    them on their own would report a cause that did not apply.

    The subtree walk that prices a matched directory already visits every file
    beneath it, so the file count — and, under `expand`, the expanded rows —
    come out of the same traversal rather than costing a second pass.

    `progress` is called with (files_seen, bytes_seen) as the walk goes, so a
    large source subvolume reports that it is moving rather than looking hung.
    Counting is nearly free next to matching, and matching is the real cost.

    Unreadable directories and vanished files are skipped, not fatal — an
    inspection must survive a source subvolume it cannot read. Skips inside a
    directory whose price was wanted mark the row `partial` rather than
    silently undercounting it.

    `pool` is an optional ProcessPoolExecutor. When given, a matched directory's
    subtree is split by top-level subdirectories and walked in parallel — on a
    RAID1 backend whose mirror selection is per-process, this reads from
    several mirrors at once.

    `skip` is a path whose subtree is pruned from the walk. The live source
    scan passes the snapshot directory here, so snapshots are never reported
    as excluded files — restic backs up the source, not its own snapshots.

    `top_level` enables parallel dispatch of non-excluded directories to the
    pool. Only the outermost call sets this; workers recurse serially within
    their own subtree, so no worker ever waits on another worker.

    `shallow` prices matched directories the way restic's walker sees them:
    pruned, unread. A shallow row carries `unknown=True` and no counts, which
    is why the default `show` costs only one readdir for directories restic
    would skip anyway. `False` (the library default) descends and prices, the
    exact totals the `--price` escape hatch asks for.

    `reveal` is a set of candidate patterns evaluated on top of `patterns`. A
    directory matched by a reveal pattern (or by a configured rule with the
    same text — revealed all along) is *not* pruned: the walk descends it and
    prices its whole subtree, listing every file. Direct children the config
    already excludes are the exception: they stay on their own unpriced rows —
    they are the part the candidate pattern does not add — and mark the
    revealed directory `partial`. Deeper rules (restic applies patterns in
    order and the config's come first) are not consulted again inside: restic
    pruned the revealed directory, so nothing beneath it was ever evaluated.
    """
    root = Path(root)
    base = clean_path(backup_root)
    seen = [0, 0]
    reveal_texts = {p.original for p in reveal}

    def tick(size: int) -> None:
        if progress is not None:
            seen[0] += 1
            seen[1] += size
            progress(seen[0], seen[1])

    def list_children(path: Path) -> tuple[list, list, bool]:
        """`(subdir entries, non-dir entries, ok)` beneath `path`.

        `ok` is False when the directory itself could not be read; the caller
        decides what that does to its row. `is_dir` stays inside the try: a
        DirEntry for a file that vanished between listing and classification
        must degrade to an empty listing, not raise out of the walk.
        """
        try:
            with os.scandir(path) as it:
                entries = list(it)
            return (
                [e for e in entries if e.is_dir(follow_symlinks=False)],
                [e for e in entries if not e.is_dir(follow_symlinks=False)],
                True,
            )
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            return [], [], False

    def detail_row(path: str, size: int, rule: str | None) -> WalkedPath:
        """An expanded per-file row, named from the walk root."""
        return WalkedPath(
            f"{base}/{os.path.relpath(path, root)}", size, False, rule, 1,
            detail=True,
        )

    def price_pieces(
        subdirs: list[str], top_files: list, rule: str | None, acc: list
    ) -> Iterator[WalkedPath]:
        """Price the listed pieces of one matched directory.

        Yields the expanded detail rows when `expand`; `acc` — `[files, bytes,
        misses]` — is updated in place, because a generator cannot also return
        the totals without pushing every caller through StopIteration
        unwrapping. A miss is an entry that could not be read or vanished
        mid-walk: the caller turns a nonzero count into a `partial` row rather
        than silently undercounting.

        Whether the pieces are chunked to the pool is decided here, next to
        both of the loops it governs: chunked when there is a pool and more
        than one subdirectory, serial otherwise — the same file set either way.
        """
        if pool is not None and len(subdirs) > 1:
            chunk_futures = [
                pool.submit(_walk_chunk_expanded if expand else _walk_chunk, sd)
                for sd in subdirs
            ]
            for fut in chunk_futures:
                if expand:
                    paths, sizes, missed = fut.result()
                    for p, s in zip(paths, sizes):
                        acc[0] += 1
                        acc[1] += s
                        tick(s)
                        yield detail_row(p, s, rule)
                else:
                    c, t, missed = fut.result()
                    acc[0] += c
                    acc[1] += t
                    tick(t)
                acc[2] += missed
        else:
            for sd in subdirs:
                missed: list = []
                for fpath, fsize in _subtree_files(Path(sd), missed):
                    acc[0] += 1
                    acc[1] += fsize
                    tick(fsize)
                    if expand:
                        yield detail_row(str(fpath), fsize, rule)
                acc[2] += len(missed)
        for e in top_files:
            try:
                size = 0 if e.is_symlink() else os.lstat(e.path).st_size
            except OSError:
                acc[2] += 1
                continue
            acc[0] += 1
            acc[1] += size
            tick(size)
            if expand:
                yield detail_row(e.path, size, rule)

    def scan(directory: Path, rel: str) -> Iterator[WalkedPath]:
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: e.name)
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            return

        def drain():
            for fut in futures:
                for (path, size, is_dir, r, files, detail,
                     unknown, partial) in fut.result():
                    yield WalkedPath(
                        path, size, is_dir, r, files, detail=detail,
                        unknown=unknown, partial=partial,
                    )
            futures.clear()

        futures = []
        for entry in entries:
            child_rel = f"{rel}/{entry.name}" if rel else entry.name
            child_path = f"{base}/{child_rel}"
            if skip and entry.path == skip:
                continue
            try:
                # follow_symlinks=False: a symlink is stored as a link, never
                # descended into.
                is_dir = entry.is_dir(follow_symlinks=False)
                hit, rule = list_match(patterns, child_path)
                if reveal:
                    reveal_hit, reveal_rule = list_match(reveal, child_path)
                else:
                    reveal_hit, reveal_rule = False, None
            except (OSError, BadPatternError):
                # Vanished or unreadable; or a pattern restic would reject
                # outright, which --lint reports.
                continue
            # Which rule decided decides what a row means. The config's rules
            # come first, exactly as restic receives them: a configured rule
            # with a different text that matched first has already excluded the
            # entry, so the candidate pattern adds nothing and wins nothing
            # here. A candidate whose text duplicates a configured rule *is*
            # that rule, revealed.
            deep = (
                (reveal_hit and not hit)
                or (hit and rule is not None and rule in reveal_texts)
            )
            if reveal_hit and not hit:
                hit, rule = True, reveal_rule
            if not attribute_rules:
                rule = None
            if is_dir:
                if deep:
                    partial = False
                    subdirs, top_files, ok = list_children(entry.path)
                    if not ok:
                        partial = True
                    priced = []
                    for e in subdirs:
                        # A direct child the config excludes is not what the
                        # candidate adds: own row, and not priced into the
                        # reveal rule.
                        sub_path = f"{child_path}/{e.name}"
                        try:
                            s_hit, s_rule = list_match(patterns, sub_path)
                        except (OSError, BadPatternError):
                            # Vanished mid-scan: its price was wanted and will
                            # now stay unknown.
                            partial = True
                            continue
                        if s_hit and not (
                            s_rule is not None and s_rule in reveal_texts
                        ):
                            yield WalkedPath(
                                sub_path, 0, True,
                                s_rule if attribute_rules else None,
                                0, unknown=True,
                            )
                            partial = True
                            continue
                        priced.append(e.path)
                    acc = [0, 0, 0]
                    yield from price_pieces(priced, top_files, rule, acc)
                    yield WalkedPath(
                        child_path, acc[1], True, rule, acc[0],
                        partial=partial or acc[2] > 0,
                    )
                    continue
                if hit and shallow:
                    # Restic's walker prunes regardless of what the subtree
                    # holds, so the report needn't read it either: one row,
                    # unknown totals, and the whole subtree skipped.
                    yield WalkedPath(child_path, 0, True, rule, 0, unknown=True)
                    continue
                if hit:
                    subdirs, top_files, ok = list_children(entry.path)
                    skipped = [] if ok else [entry.path]
                    acc = [0, 0, 0]
                    yield from price_pieces(subdirs, top_files, rule, acc)
                    yield WalkedPath(
                        child_path, acc[1], True, rule, acc[0],
                        partial=bool(acc[2] or skipped),
                    )
                    continue
                if top_level and pool is not None:
                    # The worker re-roots at `child_rel`, not at `base`: it has
                    # no idea how deep it is, so passing the backup root made
                    # every row come back with its parent directories missing.
                    futures.append(pool.submit(
                        _scan_subtree,
                        (entry.path, f"{base}/{child_rel}",
                         attribute_rules, expand, skip, shallow),
                    ))
                    # Submission order, not completion order: a reproducible
                    # walk is a reproducible report.
                    if len(futures) >= _MAX_OUTSTANDING:
                        yield from drain()
                else:
                    yield from scan(Path(entry.path), child_rel)
            elif hit:
                try:
                    size = _node_size(entry.path, entry.is_symlink())
                except OSError:
                    continue
                tick(size)
                yield WalkedPath(child_path, size, False, rule, 1)
        yield from drain()

    yield from scan(root, "")


# --------------------------------------------------------------------------
# Source 2: files already stored in restic (`restic ls --long`)
# --------------------------------------------------------------------------
# Six fields, then the path — which may hold spaces, so never whitespace-split.
# A symlink prints as `path -> target` under Go's capital-`L` mode; keeping the
# arrow made the path match nothing, so every symlink vanished from --repo.


def parse_ls_long(text: str) -> Iterator[tuple[str, int, bool]]:
    """Parse `restic ls --long` output into (path, size, is_dir).

    Six fields then the path, never whitespace-split, so names with spaces
    survive. A non-integer size is skipped rather than guessed at.
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
        is_dir = mode.startswith("d")
        # Go renders a symlink as `Lrwxrwxrwx`: capital L, unlike the lower-case
        # `l` a mode string would suggest.
        if mode[:1] in ("l", "L"):
            path = path.rsplit(" -> ", 1)[0]
        yield path, size, is_dir


def collect_from_listing(
    paths: Iterable[tuple[str, int, bool]],
    snapshot: str,
    patterns: Sequence[Pattern],
    into: ExclusionSet | None = None,
    attribute_rules: bool = False,
    detail_into: ExclusionSet | None = None,
) -> ExclusionSet:
    """Filter a stored file listing through `patterns`, accumulating an ExclusionSet.

    Excluded directories collapse into a single row carrying their subtree's
    bytes, mirroring the walk (restic prunes rather than descending). `snapshot`
    labels the per-snapshot column.

    Two passes, so the result does not depend on listing order: first find the
    directories that match (these prune their subtrees), then credit each path
    to the shallowest directory claiming it, or make it a row of its own.

    Unlike the filesystem walk, every file in the listing is already in hand, so
    the file total is exact and free. A directory — the matched one, and any
    nested one beneath it — is never counted as a file, only as the container for
    the entries under it. Symlinks are counted: restic stores each one as its own
    node, at size 0, which is what the listing already reports.

    `detail_into` is a second set that receives one row per file rather than per
    claimed directory, for the verbose report. It costs nothing here — unlike the
    filesystem walk, the listing already holds every path — and a file inside a
    claimed directory is credited to that directory's rule, because restic pruned
    the directory and no pattern ever reached the file itself.
    """
    result = into if into is not None else ExclusionSet()
    result.attribute_rules = attribute_rules
    items = list(paths)

    matched_dirs: list[str] = []
    dir_rules: dict[str, str | None] = {}
    for path, _size, is_dir in items:
        if not is_dir:
            continue
        try:
            hit, rule = list_match(patterns, path)
        except BadPatternError:
            continue
        if hit:
            matched_dirs.append(path)
            dir_rules.setdefault(path, rule if attribute_rules else None)

    for path, size, is_dir in items:
        parent = _claiming_directory(matched_dirs, path)
        if parent is not None:
            result.add(
                snapshot,
                parent,
                size,
                True,
                dir_rules.get(parent),
                0 if is_dir else 1,
            )
            if detail_into is not None and not is_dir:
                detail_into.add(
                    snapshot, path, size, False, dir_rules.get(parent), 1
                )
            continue
        try:
            hit, rule = list_match(patterns, path)
        except BadPatternError:
            continue
        if hit:
            result.add(
                snapshot,
                path,
                size,
                is_dir,
                rule if attribute_rules else None,
                0 if is_dir else 1,
            )
            if detail_into is not None and not is_dir:
                detail_into.add(
                    snapshot,
                    path,
                    size,
                    False,
                    rule if attribute_rules else None,
                    1,
                )
    return result


def patterns_digest(patterns: Sequence[str]) -> str:
    """A short stable digest of a pattern list.

    The completion stamp stores this, so a change to the rules invalidates a
    previous "already applied" mark. Patterns are NUL-joined before hashing, so
    `["ab", "c"]` and `["a", "bc"]` cannot collide. Non-UTF-8 bytes in a pattern
    (possible on Linux) survive via surrogateescape.
    """
    joined = "\0".join(patterns)
    return hashlib.sha256(joined.encode("utf-8", "surrogateescape")).hexdigest()[:16]

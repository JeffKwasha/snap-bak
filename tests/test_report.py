# -*- coding: utf-8 -*-
"""Unit tests for snap_bak/report.py — the rule-by-source grid and its renderings.

The report's whole job is to let a reader compare sources, so most of what is
pinned here is *agreement*: the same file counted the same way whether it was
found by walking the live filesystem or by reading a restic listing, and the same
rule totalled the same way in every rendering. A report whose columns disagree
with each other is worse than no report, because it looks authoritative.
"""

import csv
import io
import json

import pytest

from snap_bak import excludes, report

WIDE = 400


def build(rules, sources, root="/bak/E", **kwargs):
    """A sheet from `(kind, [(label, [(path, size, is_dir, rule, files)])])`."""
    sets = []
    for kind, entries in sources:
        found = excludes.ExclusionSet()
        for label, rows in entries:
            for path, size, is_dir, rule, files in rows:
                found.add(label, path, size, is_dir, rule, files)
        sets.append((kind, found))
    return report.build("Test", root, rules, sets, **kwargs)


# --- unit formatting ------------------------------------------------------


def test_human_bytes():
    assert report.human_bytes(0) == "0 B"
    assert report.human_bytes(512) == "512 B"
    assert report.human_bytes(1024) == "1.0 KiB"
    assert report.human_bytes(1536) == "1.5 KiB"
    assert report.human_bytes(1024**3) == "1.0 GiB"


def test_human_bytes_edge_cases():
    assert report.human_bytes(1024**5) == "1.0 PiB"
    assert report.human_bytes(1024**6) == "1024.0 PiB"
    # One decimal above 1 KiB: 1 MiB - 1 B displays as "1.0 MiB".
    assert report.human_bytes(1024**2 - 1) == "1.0 MiB"


def test_compact_bytes_fits_four_characters():
    assert report.compact_bytes(0) == "0"
    assert report.compact_bytes(1023) == "1023"
    assert report.compact_bytes(1024) == "1.0K"
    assert report.compact_bytes(9123456) == "8.7M"
    assert report.compact_bytes(12 * 1024**3) == "12G"


def test_compact_bytes_promotes_instead_of_rounding_past_a_unit():
    # 1023.9K would be five characters, and `1024K` is a lie about the unit.
    assert report.compact_bytes(int(1023.5 * 1024)) == "1.0M"
    # A value too big for one decimal rounds instead, so the width holds.
    assert report.compact_bytes(999 * 1024) == "999K"
    assert len(report.compact_bytes(999 * 1024)) <= 4


@pytest.mark.parametrize(
    "n", [0, 1, 1023, 1024, 1024**2, 1024**3, 5 * 1024**3, 1024**4]
)
def test_compact_bytes_never_exceeds_four_characters(n):
    assert len(report.compact_bytes(n)) <= 4


# --- the grid -------------------------------------------------------------


def test_a_rule_with_no_matches_still_gets_a_row():
    sheet = build(
        ["*.tmp", "*.never-matches"],
        [("live", [(excludes.LIVE, [("/bak/E/a.tmp", 5, False, "*.tmp", 1)])])],
    )
    labels = [r.label for r in sheet.rows]
    assert labels == ["*.tmp", "*.never-matches"]
    dead = next(r for r in sheet.rows if r.label == "*.never-matches")
    assert (dead.total_files, dead.total_bytes) == (0, 0)


def test_rows_sort_largest_first():
    sheet = build(
        ["*.tmp", "big"],
        [("live", [(
            excludes.LIVE,
            [("/bak/E/a.tmp", 1, False, "*.tmp", 1),
             ("/bak/E/big", 900, False, "big", 1)],
        )])],
    )
    assert [r.label for r in sheet.rows] == ["big", "*.tmp"]


def test_one_column_per_snapshot_of_the_same_kind():
    sheet = build(
        ["*.tmp"],
        [("snaps", [
            ("2026-01-15", [("/bak/E/a.tmp", 10, False, "*.tmp", 1)]),
            ("2026-01-22", [("/bak/E/a.tmp", 20, False, "*.tmp", 2)]),
        ])],
    )
    assert [c.label for c in sheet.columns] == ["2026-01-15", "2026-01-22"]
    assert [c.heading for c in sheet.columns] == [
        "snap:2026-01-15", "snap:2026-01-22",
    ]


def test_live_sorts_before_every_snapshot():
    sheet = build(
        ["*.tmp"],
        [
            ("snaps", [("2026-01-22", [("/bak/E/a.tmp", 20, False, "*.tmp", 1)])]),
            ("live", [(excludes.LIVE, [("/bak/E/a.tmp", 30, False, "*.tmp", 1)])]),
        ],
    )
    assert [c.label for c in sheet.columns] == [
        excludes.LIVE, "2026-01-22",
    ]


def test_a_rule_missing_from_a_source_reads_zero_not_absent():
    sheet = build(
        ["*.tmp", "cache"],
        [("snaps", [("s1", [("/bak/E/a.tmp", 10, False, "*.tmp", 1)])])],
    )
    cache = next(r for r in sheet.rows if r.label == "cache")
    assert cache.cells["s1"] == (0, 0)


def test_a_rule_named_twice_is_one_row():
    """A trial's effective list is `config + additions`, so re-declaring a
    configured rule hands the table the same text twice. Two rows would double
    its total and read as a contradiction."""
    sheet = build(
        ["*.tmp", "*.iso", "*.iso"],
        [("live", [(
            excludes.LIVE,
            [("/bak/E/a.tmp", 10, False, "*.tmp", 1),
             ("/bak/E/b.iso", 20, False, "*.iso", 1)],
        )])],
        marked={"*.iso"},
    )
    assert [r.label for r in sheet.rows].count("*.iso") == 1
    assert sheet.totals().total_bytes == 30


def test_marked_rules_are_the_trial_additions():
    sheet = build(
        ["*.tmp", "*.iso"],
        [("live", [(
            excludes.LIVE,
            [("/bak/E/a.tmp", 1, False, "*.tmp", 1),
             ("/bak/E/b.iso", 1, False, "*.iso", 1)],
        )])],
        marked={"*.iso"},
    )
    marked = {r.label for r in sheet.rows if r.marked}
    assert marked == {"*.iso"}


def test_totals_row_covers_every_rule():
    sheet = build(
        ["*.tmp", "cache"],
        [("live", [(
            excludes.LIVE,
            [("/bak/E/a.tmp", 10, False, "*.tmp", 1),
             ("/bak/E/b.tmp", 20, False, "*.tmp", 1),
             ("/bak/E/cache", 400, True, "cache", 7)],
        )])],
    )
    totals = sheet.totals()
    assert totals.cells[excludes.LIVE] == (9, 430)


# --- the file sheet -------------------------------------------------------


def test_detail_paths_are_relative_to_the_mount_point():
    sheet = build(
        ["*.tmp"],
        [("live", [(
            excludes.LIVE,
            [("/bak/E/build/cache/a.tmp", 10, False, "*.tmp", 1)],
        )])],
        detail_sources=[
            ("live", _detail([("/bak/E/build/cache/a.tmp", 10)]))
        ],
    )
    assert [r.label for r in sheet.detail] == ["build/cache/a.tmp"]


def test_detail_merges_the_same_path_across_sources():
    live = _detail([("/bak/E/a.tmp", 10)], excludes.LIVE)
    repo = _detail([("/bak/E/a.tmp", 15), ("/bak/E/b.tmp", 20)], "abc12345")
    sheet = report.build(
        "Test", "/bak/E", ["*.tmp"],
        [("live", live), ("repo", repo)],
        detail_sources=[("live", live), ("repo", repo)],
    )
    row = next(r for r in sheet.detail if r.label == "a.tmp")
    assert row.cells == {excludes.LIVE: (0, 10), "abc12345": (0, 15)}
    assert row.total_bytes == 25


def test_limit_bounds_the_file_sheet_and_reports_the_remainder():
    detail = _detail([(f"/bak/E/f{i}.tmp", 1) for i in range(5)])
    sheet = report.build(
        "Test", "/bak/E", ["*.tmp"], [("live", detail)],
        detail_sources=[("live", detail)], limit=2,
    )
    assert len(sheet.detail) == 2
    assert sheet.truncated == 3


def _detail(rows, label=excludes.LIVE):
    found = excludes.ExclusionSet()
    for path, size in rows:
        found.add(label, path, size, False, "*.tmp", 1)
    return found


# --- rendering ------------------------------------------------------------


def test_console_puts_the_numbers_before_the_label():
    sheet = build(
        ["*.tmp"],
        [("live", [(
            excludes.LIVE,
            [("/bak/E/a.tmp", 9123456, False, "*.tmp", 3)],
        )])],
    )
    out = report.render([sheet], width=WIDE)
    lines = out.splitlines()
    header, row = lines[1], lines[3]  # [0] job, [1] header, [2] rule, [3] row
    assert header.index("TOTAL files") < header.index("exclude pattern")
    assert row.index("8.7M") < row.index("*.tmp")
    assert "3 /" in row, "files and bytes together"


def test_console_drops_whole_columns_and_says_what_it_dropped():
    sources = [("snaps", [
        (f"2026-01-{day:02d}", [("/bak/E/a.tmp", 10, False, "*.tmp", 1)])
        for day in range(1, 13)
    ])]
    wide = report.render([build(["*.tmp"], sources)], width=WIDE)
    assert wide.count("2026-01-") == 12

    narrow = report.render([build(["*.tmp"], sources)], width=70)
    assert "not shown" in narrow
    # The dropped columns are reported by what they held, so a fitted table never
    # reads as a complete one.
    assert "11 file(s)" in narrow or "10 file(s)" in narrow


def test_console_never_drops_the_last_column_of_a_kind():
    sources = [
        ("live", [(excludes.LIVE, [("/bak/E/a.tmp", 10, False, "*.tmp", 1)])]),
        ("repo", [("abc12345", [("/bak/E/a.tmp", 10, False, "*.tmp", 1)])]),
    ]
    out = report.render([build(["*.tmp"], sources)], width=90)
    assert "live" in out and "restic:abc12345" in out


def test_markdown_keeps_every_column_whatever_the_width():
    sources = [("snaps", [
        (f"2026-01-{day:02d}", [("/bak/E/a.tmp", 10, False, "*.tmp", 1)])
        for day in range(1, 13)
    ])]
    out = report.render([build(["*.tmp"], sources)], fmt="markdown", width=40)
    # Every snapshot is present, in both its files and its bytes column.
    assert out.count("2026-01-") == 24
    assert out.startswith("## Test — `/bak/E`")
    assert "| exclude pattern |" in out


def test_markdown_verbose_adds_the_file_sheet():
    detail = _detail([("/bak/E/a.tmp", 10)])
    sheet = report.build(
        "Test", "/bak/E", ["*.tmp"], [("live", detail)],
        detail_sources=[("live", detail)],
    )
    out = report.render([sheet], fmt="markdown")
    assert "### files, relative to the mount point" in out
    assert "| `a.tmp` |" in out


def test_csv_has_one_header_across_jobs_with_different_columns():
    sheets = [
        build(["*.tmp"], [("live", [(
            excludes.LIVE, [("/bak/E/a.tmp", 10, False, "*.tmp", 1)])])]),
        build(["*.iso"], [("repo", [(
            "abc12345", [("/bak/E/b.iso", 20, False, "*.iso", 1)])])]),
    ]
    out = report.render(sheets, fmt="csv")
    lines = out.splitlines()
    assert sum(1 for line in lines if line.startswith("job,")) == 1
    header = lines[0].split(",")
    assert "live_files" in header and "restic:abc12345_files" in header
    # The union, so one spreadsheet load covers both jobs.
    assert "live_bytes" in header and "restic:abc12345_bytes" in header


def test_csv_bytes_are_exact_integers_never_abbreviated():
    sheet = build(
        ["*.tmp"],
        [("live", [(
            excludes.LIVE, [("/bak/E/a.tmp", 9123456, False, "*.tmp", 1)])])],
    )
    rows = list(csv.DictReader(io.StringIO(report.render([sheet], fmt="csv"))))
    assert int(rows[0]["live_bytes"]) == 9123456
    assert "M" not in rows[0]["live_bytes"]


def test_csv_marks_the_total_row():
    sheet = build(
        ["*.tmp"],
        [("live", [(
            excludes.LIVE, [("/bak/E/a.tmp", 10, False, "*.tmp", 1)])])],
    )
    rows = list(csv.DictReader(io.StringIO(report.render([sheet], fmt="csv"))))
    assert [r["row"] for r in rows] == ["detail", "total"]


def test_json_is_one_document_carrying_every_source_and_the_lint():
    sheets = [build(
        ["*.tmp"],
        [("snaps", [
            ("2026-01-15", [("/bak/E/a.tmp", 10, False, "*.tmp", 1)]),
            ("2026-01-22", [("/bak/E/a.tmp", 20, False, "*.tmp", 2)]),
        ])],
    )]
    body = json.loads(
        report.render(sheets, as_json=True, lint=[{"job": "Test", "kind": "invalid"}])
    )
    assert list(body) == ["lint", "jobs"]
    assert body["lint"]["issues"][0]["kind"] == "invalid"
    job_body = body["jobs"][0]
    assert [s["label"] for s in job_body["sources"]] == [
        "2026-01-15", "2026-01-22",
    ]
    # Keyed by the full heading, so `live` and `restic:`/`snap:` never collide.
    assert job_body["rules"][0]["sources"]["snap:2026-01-22"] == {
        "files": 2, "bytes": 20,
    }
    assert job_body["totals"] == {"rules": 1, "files": 3, "bytes": 30}


def test_json_reports_a_rule_matching_nothing():
    body = json.loads(report.render([build(["*.never"], [])], as_json=True))
    rule = body["jobs"][0]["rules"][0]
    assert rule["rule"] == "*.never"
    assert rule["total_files"] == 0 and rule["total_bytes"] == 0


def test_no_rules_and_no_sources_is_still_a_readable_table():
    out = report.render([build([], [])], width=WIDE)
    assert "TOTAL" in out
    body = json.loads(report.render([build([], [])], as_json=True))
    assert body["jobs"][0]["rules"] == []
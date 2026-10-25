"""One report, four renderings: what each exclude rule costs, per source.

A rule dropping 40k files live but 3 from restic has either not been deployed or
has stopped matching, and the difference is invisible unless both are on one
page. So: one row per rule, one `files / bytes` column pair per source.

Every configured rule gets a row, *including one that matches nothing* — the zero
row is the finding, and a report that omits it cannot tell "not deployed yet"
from "stopped matching". `+` marks a rule a trial added.

Only the console is fitted to the terminal. csv, markdown and json emit every
column: a fit-to-width machine format is how a reader silently gets a subset.
A file is any non-directory entry restic stores — regular file, symlink or
hardlink; directories are never counted.
"""

from __future__ import annotations

import io
import json
import shutil
from dataclasses import dataclass, field

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from snap_bak import excludes
from snap_bak.excludes import RuleTotals

__all__ = [
    "FORMATS",
    "Column",
    "Row",
    "Sheet",
    "build",
    "compact_bytes",
    "human_bytes",
    "render",
]

KIND_LIVE = "live"
KIND_SNAPS = "snaps"
KIND_REPO = "repo"

_KIND_PREFIX = {KIND_LIVE: "", KIND_SNAPS: "snap:", KIND_REPO: "restic:"}
FORMATS = ("text", "csv", "markdown")

_UNITS = "KMGTP"
# Beyond this a label is folded onto extra lines rather than allowed to push the
# numbers off screen: the numbers are the report, the label is a label.
_MAX_LABEL = 60


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


def compact_bytes(n: int) -> str:
    """A byte count in at most 4 characters: `0`, `987`, `9.1M`, `12G`.

    Fixed width per cell, so a column lines up without a padding guess. One
    decimal only where it fits; anything that rounds past its unit is promoted
    rather than printed as `1024K`.
    """
    n = int(n)
    if n < 1024:
        return str(n)
    value = float(n)
    idx = -1
    while value >= 1024 and idx < len(_UNITS) - 1:
        value /= 1024
        idx += 1
    # Promote rather than round past a unit: `999.9K` is five characters, and
    # `1024K` names the wrong unit.
    if value >= 999.5 and idx < len(_UNITS) - 1:
        value /= 1024
        idx += 1
    text = f"{value:.1f}"
    if len(text) > 3:  # `12.0` and `999.4` both fit better without the decimal
        text = f"{value:.0f}"
    return f"{text}{_UNITS[idx]}"


@dataclass(frozen=True)
class Column:
    """One source's contribution: a `files` and a `bytes` cell for every rule."""

    kind: str
    label: str
    files: int = 0
    bytes: int = 0

    @property
    def heading(self) -> str:
        return f"{_KIND_PREFIX[self.kind]}{self.label}"


@dataclass
class Row:
    """One line of the report: a rule, or — verbosely — a single file."""

    label: str
    marked: bool = False
    is_total: bool = False
    cells: dict[str, tuple[int, int]] = field(default_factory=dict)
    # Per-source caveat on the numbers: "?" marks a cell the walk could not
    # price at all (a directory pruned unread, restic-style), "+?" a price that
    # covers only part of what the rule matched. One spelling for each, always.
    rough: dict[str, str] = field(default_factory=dict)

    @property
    def total_files(self) -> int:
        return sum(f for f, _b in self.cells.values())

    @property
    def total_bytes(self) -> int:
        return sum(b for _f, b in self.cells.values())

    @property
    def display(self) -> str:
        return f"+ {self.label}" if self.marked else self.label


@dataclass
class Sheet:
    """A finished report for one job: its columns, its rows, and its totals."""

    job: str
    root: str
    columns: list[Column]
    rows: list[Row]
    detail: list[Row] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    truncated: int = 0

    def totals(self, detail: bool = False) -> Row:
        """The footer: every rule's files and bytes, summed per column.

        A caveat any contributing rule carries belongs on the total too, decided
        with the same rule the per-source cells use: nothing priced means `?`,
        some priced alongside something unpriced means `+?`.
        """
        if detail:
            cells = {
                c.label: (
                    0,
                    sum(
                        r.cells.get(c.label, (0, 0))[1] for r in self.detail
                    ),
                )
                for c in self.columns
            }
            return Row(label="TOTAL", is_total=True, cells=cells)
        rough: dict[str, str] = {}
        cells = {}
        for c in self.columns:
            t = excludes.RuleTotals(c.label)
            for r in self.rows:
                files, nbytes = r.cells.get(c.label, (0, 0))
                t.merge(
                    excludes.RuleTotals(
                        c.label,
                        files=files,
                        bytes=nbytes,
                        unknown=r.rough.get(c.label) == "?",
                        partial=r.rough.get(c.label) == "+?",
                    )
                )
            cells[c.label] = (t.files, t.bytes)
            if t.rough is not None:
                rough[c.label] = t.rough
        return Row(label="TOTAL", is_total=True, cells=cells, rough=rough)


def build(
    job: str,
    root: str,
    rules: list[str],
    sources: list[tuple[str, excludes.ExclusionSet]],
    detail_sources: list[tuple[str, excludes.ExclusionSet]] | None = None,
    limit: int | None = None,
    marked: set[str] | None = None,
) -> Sheet:
    """The report for one job.

    `rules` is the effective pattern list, so a rule that matched nothing still
    gets a row. `sources` pairs each kind with the set that scanned it; a set
    spanning several snapshots yields a column each.

    `detail_sources` are the same sources with matched directories opened up.
    They are scanned separately because a pruned directory is unrecoverable
    afterwards, and because the rule table wants one row where the file sheet
    wants the contents. Paths are relative to the mount point, so a file in three
    sources appears once rather than three times.
    """
    marked = marked or set()
    columns: list[Column] = []
    per_column_grids: list[dict[str, dict[str, RuleTotals]]] = []
    # Live first overall, not merely within its own scan: it is the source whose
    # numbers a reader checks the others against, and a column order that shuffled
    # it by which mode ran first would make the grid harder to read, not easier.
    for kind, found in sorted(sources, key=lambda s: s[0] != KIND_LIVE):
        for label in _ordered_labels(found):
            columns.append(
                Column(kind, label, found.files_in(label), found.size_in(label))
            )
            per_column_grids.append(found.by_rule_per_source([label]))

    merged: dict[str, dict[str, excludes.RuleTotals]] = {}
    for grid in per_column_grids:
        for rule, cells in grid.items():
            slot = merged.setdefault(rule, {})
            for label, totals in cells.items():
                t = slot.get(label)
                if t is None:
                    slot[label] = totals
                else:
                    # One label under one rule but two sources can only happen
                    # when two sets were handed the same label; the totals fold
                    # with their caveats exactly as the per-source cells do.
                    t.merge(totals)

    # One row per *distinct* rule, first occurrence winning. A trial's effective
    # list is `config + additions`, and a rule named in both — the trial
    # re-declaring a rule the config already has — reaches the table twice. Two
    # rows for one rule would double its total and read as a contradiction.
    rows = [
        Row(label=rule, marked=rule in marked)
        for rule in dict.fromkeys(rules)
    ]
    for rule in sorted(merged):
        if rule not in {r.label for r in rows}:
            # Attributed to no configured rule — only reachable when a set was
            # built without attribution. Surfaced rather than dropped: bytes
            # that cannot be explained are the ones worth noticing.
            rows.append(Row(label=rule))
    # Every row carries a cell for every column, zeros included: "this rule
    # matched nothing here" is a fact, and leaving it as a missing key would make
    # every renderer re-derive it.
    labels = {c.label for c in columns}
    for row in rows:
        source = merged.get(row.label, {})
        row.cells = {
            label: (
                (source[label].files, source[label].bytes)
                if label in source else (0, 0)
            )
            for label in labels
        }
        row.rough = {
            label: t.rough
            for label, t in source.items()
            if label in labels and t.rough is not None
        }
    rows.sort(key=lambda r: (-r.total_bytes, r.label))

    detail = _detail_rows(detail_sources or [], root)
    truncated = 0
    if limit is not None and len(detail) > limit:
        truncated = len(detail) - limit
        detail = detail[:limit]

    return Sheet(job=job, root=root, columns=columns, rows=rows,
                 detail=detail, truncated=truncated)


def _ordered_labels(found: excludes.ExclusionSet) -> list[str]:
    """Snapshot labels in scan order, with live first when it is present.

    Restic short-ids are hex and do not sort chronologically, so the order the
    caller scanned in is preserved rather than re-derived from the label.
    """
    return list(found.per_snapshot)


def _detail_rows(
    sources: list[tuple[str, excludes.ExclusionSet]], root: str
) -> list[Row]:
    """One row per excluded path, bytes per source."""
    merged: dict[str, dict[str, int]] = {}
    for _kind, found in sources:
        for label in found.per_snapshot:
            for path in found.per_snapshot[label]:
                bucket = merged.setdefault(path, {})
                bucket[label] = bucket.get(label, 0) + found.size_in_of(path, label)
    prefix = root.rstrip("/") + "/"
    rows = [
        Row(label=path.removeprefix(prefix),
            cells={label: (0, nbytes) for label, nbytes in cells.items()})
        for path, cells in merged.items()
    ]
    # Sorted like the rule rows: the walk order differs between the serial and
    # parallel paths, and a file sheet that reorders per run is not comparable.
    rows.sort(key=lambda r: (-r.total_bytes, r.label))
    return rows


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render(
    sheets: list[Sheet],
    fmt: str = "text",
    as_json: bool = False,
    width: int | None = None,
    lint: list | None = None,
) -> str:
    """The report in one of the four shapes.

    `width` is the console width to fit into; `None` means detect the terminal.
    It is ignored by every format but the console, because a document a terminal
    cannot display is still a document.
    """
    if as_json:
        return _json(sheets, lint or [])
    if fmt == "csv":
        return _csv(sheets)
    if fmt == "markdown":
        return _markdown(sheets)
    return _console(sheets, width)


def _console(sheets: list[Sheet], width: int | None) -> str:
    """A rich table per job, fitted to the terminal.

    Whole source columns are dropped oldest-first until the table fits, never
    truncated mid-number, and what they held is reported underneath. A fitted
    table that looks complete is worse than a narrow one that admits what it left
    out.
    """
    width = width or shutil.get_terminal_size((100, 24)).columns
    buffer = io.StringIO()
    console = Console(
        width=max(width, 40), file=buffer, force_terminal=False,
        no_color=True, highlight=False,
    )
    for index, sheet in enumerate(sheets):
        if index:
            console.print()
        shown, hidden = _fit_columns(sheet, width)
        console.print(Text(f"{sheet.job}  {sheet.root}", style="bold"))
        console.print(_rule_table(sheet, shown))
        if sheet.detail:
            console.print()
            console.print(_file_table(sheet, shown))
        for note in sheet.notes:
            console.print(Text(note, style="dim"))
        if hidden:
            console.print(Text(_hidden_note(hidden), style="dim"))
    return buffer.getvalue().rstrip("\n")


def _hidden_note(hidden: list[Column]) -> str:
    files = sum(c.files for c in hidden)
    nbytes = sum(c.bytes for c in hidden)
    return (
        f"{len(hidden)} further source(s) not shown, holding "
        f"{compact_bytes(files)} file(s) / {compact_bytes(nbytes)} in total. "
        f"Widen the terminal, or use --format markdown or --json for every column."
    )


def _rule_table(sheet: Sheet, shown: list[Column]) -> Table:
    table = Table(box=box.SIMPLE_HEAD, expand=False, pad_edge=False,
                  header_style="bold", show_edge=False)
    table.add_column("TOTAL files", justify="right", style="bold")
    table.add_column("TOTAL bytes", justify="right", style="bold")
    for column in shown:
        table.add_column(column.heading, justify="right")
    table.add_column("exclude pattern", justify="left", overflow="fold")
    for row in [*sheet.rows, sheet.totals()]:
        total_rough = _total_rough(row)
        table.add_row(
            _num(row.total_files, total_rough),
            _num(row.total_bytes, total_rough),
            *(_cell(row, column) for column in shown),
            row.display,
        )
    return table


def _file_table(sheet: Sheet, shown: list[Column]) -> Table:
    table = Table(box=box.SIMPLE_HEAD, expand=False, pad_edge=False,
                  header_style="bold", show_edge=False)
    table.add_column("TOTAL", justify="right", style="bold")
    for column in shown:
        table.add_column(column.heading, justify="right")
    table.add_column("path (relative to the mount point)", justify="left",
                     overflow="fold")
    rows = [*sheet.detail, sheet.totals(detail=True)]
    if sheet.truncated:
        rows.append(Row(label=f"... {sheet.truncated} more (totals cover all)"))
    for row in rows:
        table.add_row(
            compact_bytes(row.total_bytes),
            *(compact_bytes(row.cells.get(c.label, (0, 0))[1]) for c in shown),
            row.display,
        )
    return table


def _num(value: int, rough: str | None) -> str:
    """One number cell in the split-cell formats (markdown, csv is raw ints).

    A caveat attaches to the *price*, which only the bytes carry: `?` when the
    walk could not price it at all, `+?` when the price covers only part. The
    files cell is `?` alongside it — no number there is knowable either.
    """
    if rough == "?":
        return "?"
    text = compact_bytes(value)
    return f"{text}+?" if rough == "+?" else text


def _cell(row: Row, column: Column) -> str:
    rough = row.rough.get(column.label)
    if rough == "?":
        return "?"
    # Each number carries the caveat individually — the same `_num` the split
    # formats use — so a partial price reads `2+? / 140+?` everywhere it prints.
    files, nbytes = row.cells.get(column.label, (0, 0))
    return f"{_num(files, rough)} / {_num(nbytes, rough)}"


def _fit_columns(sheet: Sheet, width: int) -> tuple[list[Column], list[Column]]:
    """Drop whole source columns, oldest first, until the table fits `width`.

    A column is only droppable while its kind survives the drop: a report that
    lost the repository column would be answering a different question than the
    one asked.
    """
    columns = list(sheet.columns)
    all_kinds = {c.kind for c in sheet.columns}
    label_width = min(
        max([len(r.display) for r in sheet.rows]
            + [len(r.label) for r in sheet.detail]
            + [len("exclude pattern")]),
        _MAX_LABEL,
    )

    def width_of(kept: list[Column]) -> int:
        # A column is as wide as its heading or as its cells, whichever is
        # larger — and a cell is always `files / bytes`, so at most 11.
        per_column = sum(max(len(c.heading), 11) for c in kept)
        return per_column + 11 + 11 + label_width + 3 * len(kept)

    while width_of(columns) > width:
        victim = next(
            (
                i
                for i in range(len(columns) - 1, -1, -1)
                if all_kinds <= {c.kind for c in columns[:i] + columns[i + 1:]}
            ),
            None,
        )
        if victim is None:
            break  # one column per kind is what the caller asked for
        columns.pop(victim)
    hidden = [c for c in sheet.columns if c not in columns]
    return columns, hidden


def _markdown(sheets: list[Sheet]) -> str:
    """GitHub-flavoured tables. Every column, always."""
    out: list[str] = []
    for sheet in sheets:
        out += [f"## {sheet.job} — `{sheet.root}`", ""]
        out += _md_table(sheet, detail=False)
        if sheet.detail:
            out += ["", "### files, relative to the mount point", ""]
            out += _md_table(sheet, detail=True)
        if sheet.notes:
            out += [""] + [f"- {n}" for n in sheet.notes]
        out.append("")
    return "\n".join(out).rstrip("\n")


def _total_rough(row: Row) -> str | None:
    """The caveat on a row's TOTAL cells, decided the same way cell merge is.

    Nothing priced across the caveat-carrying columns is `?`; priced numbers
    alongside any caveat are `+?`.
    """
    if not row.rough:
        return None
    t = excludes.RuleTotals(row.label)
    for label, rough in row.rough.items():
        files, nbytes = row.cells.get(label, (0, 0))
        t.merge(
            excludes.RuleTotals(
                label, files=files, bytes=nbytes,
                unknown=rough == "?", partial=rough == "+?",
            )
        )
    return t.rough


def _md_table(sheet: Sheet, detail: bool) -> list[str]:
    header = ["exclude pattern"]
    for column in sheet.columns:
        header += [f"{column.heading} files", f"{column.heading} bytes"]
    header += ["total files", "total bytes"]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] + ["---:"] * (len(header) - 1)) + " |",
    ]
    body = sheet.detail if detail else [*sheet.rows, sheet.totals()]
    for row in body:
        cells = [f"`{row.display}`"]
        total_rough = _total_rough(row)
        for column in sheet.columns:
            files, nbytes = row.cells.get(column.label, (0, 0))
            rough = row.rough.get(column.label) if not row.is_total else total_rough
            cells += [_num(files, rough), _num(nbytes, rough)]
        # The files and the bytes total carry the same caveat: a SUMMARY's
        # incompleteness is one fact, not two.
        cells.append(_num(row.total_files, total_rough))
        cells.append(_num(row.total_bytes, total_rough))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _csv(sheets: list[Sheet]) -> str:
    """Raw integers, one header, every column any job has.

    Two jobs rarely share a snapshot set, so the header is the union of every
    job's columns: one table a spreadsheet can load, rather than one header per
    job that no importer accepts.
    """
    import csv

    columns: list[Column] = []
    seen: set[tuple[str, str]] = set()
    for sheet in sheets:
        for column in sheet.columns:
            if (column.kind, column.label) not in seen:
                seen.add((column.kind, column.label))
                columns.append(column)

    fieldnames = ["job", "root", "scope", "row", "rule_or_path"]
    for column in columns:
        fieldnames += [f"{column.heading}_files", f"{column.heading}_bytes"]
    fieldnames += ["total_files", "total_bytes"]

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fieldnames)
    writer.writeheader()
    for sheet in sheets:
        blocks = [("rule", sheet.rows, sheet.totals())]
        if sheet.detail:
            blocks.append(("file", sheet.detail, sheet.totals(detail=True)))
        for scope, rows, total in blocks:
            for row in [*rows, total]:
                record = {
                    "job": sheet.job,
                    "root": sheet.root,
                    "scope": scope,
                    "row": "total" if row.is_total else "detail",
                    "rule_or_path": row.label,
                    "total_files": row.total_files,
                    "total_bytes": row.total_bytes,
                }
                for column in columns:
                    files, nbytes = row.cells.get(column.label, (0, 0))
                    # A caveated cell is not a number: an unpriced total writes
                    # empty rather than `0` (which would claim "scanned and
                    # priced, nothing there"). A partial price's known part is
                    # a real integer; the sheet's note tells the reader the
                    # `+?` story the prose renderings show.
                    rough = row.rough.get(column.label)
                    record[f"{column.heading}_files"] = (
                        None if rough == "?" else files
                    )
                    record[f"{column.heading}_bytes"] = (
                        None if rough == "?" else nbytes
                    )
                writer.writerow(record)
        for note in sheet.notes:
            writer.writerow(
                {"job": sheet.job, "scope": "note", "row": "note",
                 "rule_or_path": note}
            )
    return out.getvalue()


def _json(sheets: list[Sheet], lint: list) -> str:
    """The whole grid, structurally. Every column, every row, no fitting.

    One document for the whole invocation — lint, every job, every source — so
    an agent parses one object instead of guessing how many streams it is going
    to be handed.
    """
    return json.dumps(
        {
            "lint": {"issues": lint},
            "jobs": [_sheet_json(sheet) for sheet in sheets],
        },
        indent=2,
    )


def _totals_json(sheet: Sheet) -> dict:
    """The footer's JSON. `"rough"` appears only when a caveat exists, so
    fully-priced documents keep the shape they always had."""
    total = sheet.totals()
    out: dict[str, object] = {
        "rules": len(sheet.rows),
        "files": total.total_files,
        "bytes": total.total_bytes,
    }
    if total.rough:
        headings = {c.label: c.heading for c in sheet.columns}
        out["rough"] = {
            headings.get(label, label): flag
            for label, flag in total.rough.items()
        }
    return out


def _rule_json(row: Row, columns: list[Column]) -> dict:
    """One rule row's JSON. A caveat's `rough` key appears only when set, so a
    fully-priced document keeps the shape it always had."""
    sources = {}
    for c in columns:
        entry: dict[str, object] = {
            "files": row.cells.get(c.label, (0, 0))[0],
            "bytes": row.cells.get(c.label, (0, 0))[1],
        }
        rough = row.rough.get(c.label)
        if rough is not None:
            entry["rough"] = rough
        sources[c.heading] = entry
    out = {
        "rule": row.label,
        "added_by_trial": row.marked,
        "sources": sources,
        "total_files": row.total_files,
        "total_bytes": row.total_bytes,
    }
    total_rough = _total_rough(row)
    if total_rough is not None:
        out["rough"] = total_rough
    return out


def _sheet_json(sheet: Sheet) -> dict:
    return {
        "job": sheet.job,
        "root": sheet.root,
        "sources": [
            {"kind": c.kind, "label": c.label, "files": c.files, "bytes": c.bytes}
            for c in sheet.columns
        ],
        "rules": [
            _rule_json(row, sheet.columns)
            for row in sheet.rows
        ],
        "files": [
            {
                "path": row.label,
                "sources": {
                    c.heading: row.cells.get(c.label, (0, 0))[1]
                    for c in sheet.columns
                },
                "total_bytes": row.total_bytes,
            }
            for row in sheet.detail
        ],
        "files_omitted": sheet.truncated,
        "totals": _totals_json(sheet),
        "notes": sheet.notes,
    }
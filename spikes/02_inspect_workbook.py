"""
SPIKE 02 - What is actually inside the roster workbook?

Throwaway code. It answers one question: how is the multi-batch sheet laid
out, so that a real parser can find batch blocks by MARKER rather than by
hardcoded row number. Row numbers shift the moment a batch gains a student;
markers do not.

Read-only. Opens the workbook, prints a structure report, writes nothing.

    python spikes/02_inspect_workbook.py
    python spikes/02_inspect_workbook.py --names   # unmask student names

Student names and enrollment IDs are MASKED by default. This report gets
pasted into chat logs and issues; the roster does not need to travel with it.
"""

import argparse
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parent.parent
BOOK = ROOT / "Google Attendance Management System FMO Jul-2026.xlsx"

# Markers that delimit a batch block. Matched case-insensitively against the
# stripped cell text, anywhere in the row - we do NOT assume a column.
BLOCK_START = "attendance sheet"
HEADER_MARK = "s. no"
FOOTER_MARKS = [
    "module",
    "session covered as per cpc",
    "actual session covered",
    "total flow",
    "faculty name",
]

# Anything that looks like an enrollment id, so the mask catches ids that
# turn up in unexpected columns.
ENROLL_RE = re.compile(r"\b[A-Za-z]*\d{6,}\b")


def txt(v) -> str:
    return "" if v is None else str(v).strip()


def norm(v) -> str:
    return txt(v).lower().replace(" ", " ")


def mask_name(v: str) -> str:
    """'FIRST LAST' -> 'F**** L***'. Keeps shape, drops identity."""
    parts = [p for p in v.split() if p]
    return " ".join(p[0] + "*" * (len(p) - 1) for p in parts) or "?"


def mask_id(v: str) -> str:
    return ENROLL_RE.sub(lambda m: m.group(0)[:2] + "*" * (len(m.group(0)) - 2), v)


def find_rows(grid, needle):
    """Row numbers (1-based) containing `needle` in any cell."""
    return [r for r, row in enumerate(grid, start=1)
            if any(norm(c) == needle for c in row)]


def find_in_row(grid, r, needle):
    """1-based column of `needle` in row r, or None."""
    for c, v in enumerate(grid[r - 1], start=1):
        if norm(v) == needle:
            return c
    return None


def cell(grid, r, c):
    if r < 1 or r > len(grid) or c is None or c < 1 or c > len(grid[r - 1]):
        return None
    return grid[r - 1][c - 1]


def is_date(v) -> bool:
    return isinstance(v, (datetime, date))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--names", action="store_true", help="do not mask names/ids")
    ap.add_argument("--file", default=str(BOOK))
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        sys.exit(f"FAIL: workbook not found at {path}")

    wb = load_workbook(path, data_only=True, read_only=False)
    print(f"FILE   {path.name}  ({path.stat().st_size:,} bytes)")
    print(f"SHEETS {wb.sheetnames}\n")

    for ws in wb.worksheets:
        print("=" * 74)
        print(f"SHEET '{ws.title}'   rows={ws.max_row}  cols={ws.max_column}")
        merged = list(ws.merged_cells.ranges)
        print(f"merged ranges: {len(merged)}"
              + (f"  {[str(m) for m in merged[:8]]}" if merged else ""))
        print("=" * 74)

        grid = [list(r) for r in ws.iter_rows(values_only=True)]

        starts = find_rows(grid, BLOCK_START)
        headers = find_rows(grid, HEADER_MARK)
        print(f"\n'{BLOCK_START}' rows : {starts}")
        print(f"'{HEADER_MARK}' rows      : {headers}")
        for m in FOOTER_MARKS:
            print(f"'{m}' rows{' ' * max(0, 22 - len(m))}: {find_rows(grid, m)}")

        if not starts:
            print("\n!! no block markers found - dumping first 40 rows raw")
            for r in range(1, min(41, len(grid) + 1)):
                vals = [f"{get_column_letter(c)}={txt(v)[:24]}"
                        for c, v in enumerate(grid[r - 1], 1) if txt(v)]
                if vals:
                    print(f"  r{r:<4} {' | '.join(vals)}")
            continue

        bounds = starts + [len(grid) + 1]
        for i, top in enumerate(starts):
            end = bounds[i + 1] - 1
            report_block(grid, top, end, i + 1, args.names)


def report_block(grid, top, end, idx, show_names) -> None:
    print("\n" + "-" * 74)
    print(f"BLOCK {idx}   rows {top}..{end}")
    print("-" * 74)

    # --- header metadata: every labelled cell above the S. No row ----------
    hdr = next((r for r in range(top, end + 1)
                if find_in_row(grid, r, HEADER_MARK)), None)
    if hdr is None:
        print("  !! no 'S. No' header row in this block")
        return

    print("  metadata rows:")
    for r in range(top, hdr):
        vals = []
        for c, v in enumerate(grid[r - 1], 1):
            t = txt(v)
            if t:
                vals.append(f"{get_column_letter(c)}={mask_id(t)[:34]}")
        if vals:
            print(f"    r{r:<4} {' | '.join(vals)}")

    # --- the header row: which columns are what ----------------------------
    print(f"\n  header row r{hdr}:")
    date_cols, label_cols = [], []
    for c, v in enumerate(grid[hdr - 1], 1):
        if v is None:
            continue
        if is_date(v):
            date_cols.append((c, v))
        else:
            label_cols.append((c, txt(v)))
    for c, t in label_cols:
        print(f"    {get_column_letter(c):>3} (col {c:>2})  {t}")
    print(f"\n  date columns: {len(date_cols)}")
    for c, v in date_cols:
        d = v.date() if isinstance(v, datetime) else v
        print(f"    {get_column_letter(c):>3} (col {c:>2})  {d.isoformat()}  {d.strftime('%a')}")
    if not date_cols:
        print("    !! none parsed as dates - raw header values:")
        for c, v in enumerate(grid[hdr - 1], 1):
            if txt(v):
                print(f"       {get_column_letter(c)}: {txt(v)[:30]!r} ({type(v).__name__})")

    # --- student rows -------------------------------------------------------
    id_col = find_in_row(grid, hdr, "enrollment #")
    name_col = find_in_row(grid, hdr, "student name")
    course_col = find_in_row(grid, hdr, "course")
    print(f"\n  id col={id_col}  name col={name_col}  course col={course_col}")

    rows = []
    for r in range(hdr + 1, end + 1):
        sid = txt(cell(grid, r, id_col))
        nm = txt(cell(grid, r, name_col))
        if not sid and not nm:
            if rows:
                break
            continue
        rows.append(r)

    print(f"  student rows: {len(rows)}"
          + (f"  (r{rows[0]}..r{rows[-1]})" if rows else ""))

    codes, courses, per_student = Counter(), Counter(), []
    for r in rows:
        sid = txt(cell(grid, r, id_col))
        nm = txt(cell(grid, r, name_col))
        courses[txt(cell(grid, r, course_col))] += 1
        marks = []
        for c, _ in date_cols:
            t = txt(cell(grid, r, c)).upper()
            codes[t or "(blank)"] += 1
            marks.append(t or ".")
        per_student.append((sid, nm, marks))

    print(f"  courses: {dict(courses)}")
    print(f"  status codes across all cells: {dict(codes)}")

    print("\n  per-student marks:")
    for sid, nm, marks in per_student:
        s = sid if show_names else mask_id(sid)
        n = nm if show_names else mask_name(nm)
        valid = sum(1 for m in marks if m in ("P", "O"))
        flag = "  <-- OVER 14" if valid > 14 else ""
        print(f"    {s:<14} {n:<22} {''.join(m[0] if m else '.' for m in marks)}"
              f"  valid={valid:<3}{flag}")

    # --- footer rows --------------------------------------------------------
    print("\n  footer rows:")
    for r in range(rows[-1] + 1 if rows else hdr + 1, end + 1):
        vals = [f"{get_column_letter(c)}={txt(v)[:26]}"
                for c, v in enumerate(grid[r - 1], 1) if txt(v)]
        if vals:
            print(f"    r{r:<4} {' | '.join(vals)}")


if __name__ == "__main__":
    main()

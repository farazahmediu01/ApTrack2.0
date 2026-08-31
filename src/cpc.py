"""
Class Progress Chart reader.

A CPC workbook is the curriculum for one course code. It answers two questions
about a cumulative session index:

    which semester (term) is session 300 in?
    which book (module) is session 300 in?

Both answers are course-specific. The same index means different things in
different curricula, and the same abbreviation does too - `R` is R Programming
in 7062 and ReactJS in 7144. Never resolve a module name without a course.

Pure: reads a file, returns data, touches no network.
"""

from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook

# Batch Code prefix -> course code. The prefix is the only reliable curriculum
# signal on the roster; the per-student Course column disagrees with it (see
# the 7066 students sitting in PR2 batches).
PREFIX_TO_COURSE = {
    "PR2": "7062",   # ACCP-PRIME-2
    "AI": "7144",    # ACCP-AI
}


@dataclass(frozen=True)
class Span:
    """A half-open-free, inclusive range of cumulative session indices."""
    lo: int
    hi: int
    label: str
    name: str = ""      # full subject name, as the portal also spells it

    def holds(self, n: int) -> bool:
        return self.lo <= n <= self.hi


@dataclass
class Curriculum:
    course: str
    semesters: list[Span]   # CPISM, DISM, HDSE I, ... in order
    modules: list[Span]     # RPRO, FBDS, PBD, ... in teaching order

    def semester_of(self, n: int) -> Span | None:
        return next((s for s in self.semesters if s.holds(n)), None)

    def module_of(self, n: int) -> Span | None:
        return next((m for m in self.modules if m.holds(n)), None)

    def order_of(self, module_name: str) -> int | None:
        """
        Teaching position of a portal module, by its full name.

        The portal's ModuleName and the CPC's Subjects column spell books the
        same way ('Foundation of Big Data Systems'), so the CPC's session index
        gives authoritative teaching order without a mapping table.

        Returns None for anything the CPC does not contain - term exams and
        term kits, which are not classes and must never be marked.
        """
        want = _key(module_name)
        if not want:
            return None
        best, best_len = None, -1
        for m in self.modules:
            k = _key(m.name)
            if not k:
                continue
            if k == want:
                return m.lo
            # Substring matching alone is too loose: 'Processing Big Data'
            # sits inside 'eProject-Processing Big Data with Hadoop', which is
            # a different book two terms later. Keep the LONGEST match, which
            # is always the more specific title.
            if (k in want or want in k) and len(k) > best_len:
                best, best_len = m.lo, len(k)
        return best

    def term_number(self, n: int) -> int | None:
        """Semester index as a 1-based term number, which is how the sheet
        and the portal both talk about it."""
        for i, s in enumerate(self.semesters, start=1):
            if s.holds(n):
                return i
        return None


def _key(v: str) -> str:
    """Loose comparison key: letters and digits only, lowercased."""
    return "".join(ch for ch in (v or "").lower() if ch.isalnum())


def _txt(v) -> str:
    return "" if v is None else str(v).strip()


def _as_int(v):
    s = _txt(v)
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _read_semesters(ws) -> list[Span]:
    """
    Sheet 'CPC', the 'Batch Planed CPC' table. Column C is the class (session)
    number, column E the semester label - written ONCE at each boundary and
    blank thereafter, so it has to be forward-filled.

    This column is the authority for term boundaries. The 'courses' sheet also
    carries certification markers (CPISM / DISM / HDSE I ...) but they sit on
    different rows and give different boundaries; they are not trustworthy.
    """
    header = next((r for r in range(1, ws.max_row + 1)
                   if _txt(ws.cell(r, 5).value).lower() == "semester"), None)
    if header is None:
        raise ValueError("no 'Semester' header found on the CPC sheet")

    spans, label, lo, hi = [], None, None, None
    for r in range(header + 1, ws.max_row + 1):
        n = _as_int(ws.cell(r, 3).value)
        if n is None:
            continue
        seen = _txt(ws.cell(r, 5).value)
        if seen and seen != label:
            if label is not None:
                spans.append(Span(lo, hi, label))
            label, lo = seen, n
        hi = n
    if label is not None:
        spans.append(Span(lo, hi, label))

    # The tail row is padding past the end of the taught curriculum.
    return [s for s in spans if not s.label.lower().endswith("ends")]


def _read_modules(ws) -> list[Span]:
    """
    Sheet 'courses'. Column D is the cumulative session index, column E the
    session code ('MDB 03', 'VAT12'). Strip the trailing number to get the book
    abbreviation, then collapse consecutive runs into spans.
    """
    spans, code, lo, hi, name = [], None, None, None, ""
    for row in ws.iter_rows(min_row=2, values_only=True):
        n = _as_int(row[3])
        cell = _txt(row[4])
        if n is None or not cell:
            continue
        abbrev = cell.rstrip("0123456789 ").strip() or cell
        if abbrev != code:
            if code is not None:
                spans.append(Span(lo, hi, code, name))
            code, lo, name = abbrev, n, ""
        name = name or _txt(row[6])
        hi = n
    if code is not None:
        spans.append(Span(lo, hi, code, name))
    return spans


def load(path: str | Path) -> Curriculum:
    path = Path(path)
    wb = load_workbook(path, data_only=True)
    course = "".join(ch for ch in path.stem if ch.isdigit())
    return Curriculum(
        course=course,
        semesters=_read_semesters(wb["CPC"]),
        modules=_read_modules(wb["courses"]),
    )


def load_all(cpc_dir: str | Path) -> dict[str, Curriculum]:
    """course code -> Curriculum, for every CPC-NNNN.xlsx in the directory."""
    return {c.course: c
            for c in (load(p) for p in sorted(Path(cpc_dir).glob("CPC-*.xlsx")))}


def course_for_batch(batch_code: str) -> str | None:
    """'AI-202409E+08E' -> '7144'. Unknown prefixes return None, not a guess."""
    prefix = _txt(batch_code).split("-", 1)[0].upper()
    return PREFIX_TO_COURSE.get(prefix)

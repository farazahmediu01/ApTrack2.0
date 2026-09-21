"""
Domain logic: sheet + curriculum -> how many sessions each student needs.

No network, no file formats, no printing. Pure functions of parsed data, so it
can be unit-tested with literals.

The rules are documented in skills/attendance-domain-rules.md. The one that
shapes this module: what management audits is a COUNT per student per month.
Which sessions carry that count is our choice, and term boundaries may be
crossed to find them.
"""

from dataclasses import dataclass, field

from . import cpc

VALID_CODES = {"P", "O"}       # present, online
SKIP_CODES = {"A", "L", "H"}   # absent, leave, holiday - all mean "do not mark"

MONTHLY_CAP = 14

# Enrollment status is REPORTED, never enforced.
#
# Many students whose portal status reads "FDO" (Financial Drop Out) are still
# attending class - the portal record is simply stale. Skipping them was tried
# on 2026-08-31 and reverted the same day: it silenced 12 students who should
# be marked. Show the status so the teacher can see it; do not act on it.
def enrollment_note(item: dict) -> str | None:
    """A short tag for a non-standard enrollment status, or None if normal."""
    status = str(item.get("Status") or "").strip()
    if not status or status.lower() == "enrolled":
        return None
    return {"FDO": "FDO (portal status stale?)"}.get(status.upper(), status)


# The roster abbreviates books its own way. Course-scoped on purpose: `R` is
# R Programming under 7062 and ReactJS under 7144.
SHEET_ALIASES = {
    "7062": {"R": "RPRO", "FBD": "FBDS", "E-PRO": "PROJECT", "EP": "PROJECT", "E": "PROJECT"},
    "7144": {"R": "REACT", "MDB": "MONGO", "E-PRO": "EPRO", "EP": "EPRO"},
}


@dataclass
class StudentPlan:
    student_id: str
    name: str
    course: str
    want: int                       # P + O in the row, before the cap
    capped: int                     # what we will actually try to mark
    blockers: list[str] = field(default_factory=list)

    @property
    def markable(self) -> bool:
        return not self.blockers and self.capped > 0


@dataclass
class BatchPlan:
    batch_code: str
    course: str | None
    days: str
    time: str
    month: str
    start_date: object = None
    start_term: int | None = None
    start_term_source: str = ""
    books: list[str] = field(default_factory=list)
    students: list[StudentPlan] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total_wanted(self) -> int:
        return sum(s.capped for s in self.students if s.markable)


def strip_suffix(module: str) -> str:
    """'VAT12' -> 'VAT', 'E-Pro' -> 'E-PRO'."""
    return (module or "").rstrip("0123456789 ").strip().upper()


def resolve_book(module: str, cur: cpc.Curriculum) -> cpc.Span | None:
    """
    Sheet module label -> the CPC module span it names.

    Tried in order: alias table, exact match, prefix match. Returns None rather
    than guessing - an unresolved book is reported, never silently defaulted.
    """
    base = strip_suffix(module)
    if not base:
        return None
    want = SHEET_ALIASES.get(cur.course, {}).get(base, base)
    by_code = {m.label.upper(): m for m in cur.modules}
    if want in by_code:
        return by_code[want]
    hits = [m for c, m in by_code.items() if c.startswith(want) or want.startswith(c)]
    return hits[0] if len(hits) == 1 else None


def start_term(batch, cur: cpc.Curriculum) -> tuple[int | None, str, list[str]]:
    """
    Which term the batch is teaching this month.

    Derived from the Module row, NOT from 'Actual Session Covered'. The Actual
    row is unreliable in real sheets - it has been seen holding a literal 0,
    running backwards, and staying constant across a whole block - while the
    Module row has always been intact. Where Actual is sane it is used as a
    cross-check only.

    Under the fallback rules this is only a STARTING point: if the term has no
    pending sessions the marker walks to other terms anyway. So a wrong answer
    here changes which sessions get consumed, never how many.
    """
    notes, terms = [], []
    for m in batch.modules:
        span = resolve_book(m, cur)
        if span is None:
            if strip_suffix(m):
                notes.append(f"unrecognised book {m!r}")
            continue
        t = cur.term_number(span.lo)
        if t:
            terms.append(t)
    if not terms:
        return None, "", notes

    chosen = min(terms)
    if len(set(terms)) > 1:
        notes.append(f"module row spans terms {sorted(set(terms))}; starting at T{chosen}")

    # Cross-check against Actual Session Covered where it looks sane.
    sane = [n for n in batch.actual if n and n > 0]
    if sane:
        a_terms = {cur.term_number(n) for n in sane} - {None}
        if a_terms and chosen not in a_terms:
            notes.append(
                f"'Actual Session Covered' implies term(s) {sorted(a_terms)} but the "
                f"Module row implies T{chosen}; using the Module row")
    return chosen, "module row", notes


def _actual_row_health(batch) -> list[str]:
    """The Actual row is frequently corrupt. Say so loudly; do not depend on it."""
    vals = batch.actual
    bad = []
    if any(v == 0 for v in vals):
        bad.append("contains a literal 0")
    known = [v for v in vals if v]
    if len(set(known)) == 1 and len(known) > 1:
        bad.append(f"constant at {known[0]} across the whole block")
    if any(a and b and b < a for a, b in zip(known, known[1:])):
        bad.append("runs backwards")
    return bad


def plan_batch(batch, curricula: dict[str, cpc.Curriculum]) -> BatchPlan:
    course = cpc.course_for_batch(batch.batch_code)
    cur = curricula.get(course) if course else None

    plan = BatchPlan(
        batch_code=batch.batch_code, course=course,
        days=batch.days, time=batch.time, month=batch.month,
        start_date=batch.start_date,
        books=sorted({strip_suffix(m) for m in batch.modules if strip_suffix(m)}),
    )

    for msg in _actual_row_health(batch):
        plan.notes.append(f"'Actual Session Covered' {msg} - ignored, Module row used instead")

    if cur is None:
        plan.notes.append(
            f"no curriculum for batch prefix {batch.batch_code.split('-')[0]!r} - "
            "term-agnostic: sessions will be taken in portal order")
    else:
        plan.start_term, plan.start_term_source, notes = start_term(batch, cur)
        plan.notes.extend(notes)
        if plan.start_term is None:
            plan.notes.append("could not resolve a starting term from the Module row")

    for st in batch.students:
        plan.students.append(_plan_student(st))
    return plan


def _plan_student(st) -> StudentPlan:
    want = sum(1 for c in st.marks if c in VALID_CODES)
    sp = StudentPlan(student_id=st.student_id, name=st.name, course=st.course,
                     want=want, capped=min(want, MONTHLY_CAP))
    if want > MONTHLY_CAP:
        sp.blockers.append(
            f"{want} valid marks exceeds the {MONTHLY_CAP}/month cap - "
            "mark nothing, refer to the teacher")
        sp.capped = 0
    return sp


def plan_all(batches, curricula) -> list[BatchPlan]:
    return [plan_batch(b, curricula) for b in batches]


# ---------------------------------------------------------------------------
# Session selection - the fallback order. Pure; takes portal rows as dicts.
# ---------------------------------------------------------------------------

def session_number(name: str) -> int:
    """'HADOOP-21_Session07' -> 7. Sorting the string is wrong."""
    import re
    m = re.search(r"(\d+)\s*$", name or "")
    return int(m.group(1)) if m else -1


def term_order(start: int | None, present: list[int]) -> list[int]:
    """
    Which terms to consume, in order.

    Current term first, then EARLIER terms ascending, then LATER terms
    ascending. Faculty's rule: a student short of pending sessions in T5 should
    have T1..T4 used up before T6 is touched, because the earlier terms are the
    backlog that will otherwise never be filled.
    """
    terms = sorted(set(present))
    if start is None or start not in terms:
        return terms
    return ([start]
            + [t for t in terms if t < start]
            + [t for t in terms if t > start])


# Terms this script must NEVER mark.
#
# Programme structure (faculty, 2026-08-31):
#   HDSE = 2 years / 4 semesters - S1 frontend, S2 PHP+Laravel, S3 .NET, S4 Flutter
#   ADSE = 3 years / 6 semesters - the same four, then S5 MERN stack, S6 R + Big Data
#   SBTE = a separate board (English, Oracle, Maths, Physics) that some students
#          enter after the four HDSE semesters, sometimes running in parallel
#          with ADSE S5/S6.
#
# In the portal, SBTE students sit on course 7066: terms T1-T4 are identical to
# 7062 (72/79/80/72 sessions) and T5 is a single 104-session block that is the
# SBTE content. That block is out of scope - its CPC does not exist yet and a
# separate script will handle it.
EXCLUDED_TERM_CODES = {"OV-7066-T5"}


def is_excluded(session, cur=None) -> bool:
    """
    True only for SBTE. Nothing else is excluded.

    Term End Examination and Term-N-KIT sessions WERE excluded here for one
    day (2026-08-31) on the theory that they are not classes. Faculty
    corrected that on 2026-09-21: they are valid sessions that must be marked,
    two per term. The `cur` parameter is kept so callers need not change; the
    CPC now only affects ORDER (exams and kits sort after the books), never
    eligibility.
    """
    code = (session.get("TermCode") or "").upper()
    name = (session.get("TermName") or "").upper()
    return code in EXCLUDED_TERM_CODES or "SBTE" in code or "SBTE" in name


def is_exam_or_kit(session) -> bool:
    mod = (session.get("ModuleCode") or "").upper()
    return "EXAM" in mod or "KIT" in mod


def select_sessions(sessions, start_term_no, book_order, n, cur=None):
    """
    Choose n pending sessions to mark.

    `sessions` are portal rows (dicts with TermCode/ModuleCode/SessionName/
    IsPresent). Returns (chosen, shortfall).

    Within a term, sessions run book by book in teaching order, and within a
    book by the numeric suffix of SessionName - never by SessionId, never by
    string sort.
    """
    pending = [s for s in sessions
               if not s.get("IsPresent") and not is_excluded(s, cur)]
    by_term = {}
    for s in pending:
        by_term.setdefault(term_no(s), []).append(s)

    order = term_order(start_term_no, [t for t in by_term if t is not None])
    if None in by_term:
        order = order + [None]

    # Teaching order, best source first:
    #   1. the CPC's own session index, matched on the module's full name
    #   2. the student's marking history (book_order), for anything the CPC
    #      cannot place
    # History alone gets this wrong: a module with one stray early mark sorts
    # ahead of an untouched module that the CPC puts before it.
    hist = {code.upper(): i for i, code in enumerate(book_order or [])}

    def rank_of(sess):
        # exams and kits come after every book in the term, kit before exam
        if is_exam_or_kit(sess):
            return (2, 0 if "KIT" in (sess.get("ModuleCode") or "").upper() else 1)
        if cur is not None:
            pos = cur.order_of(sess.get("ModuleName"))
            if pos is not None:
                return (0, pos)
        return (1, hist.get((sess.get("ModuleCode") or "").upper(), 10_000))

    chosen = []
    for t in order:
        rows = sorted(
            by_term.get(t, []),
            key=lambda s: (rank_of(s), s.get("ModuleCode") or "",
                           session_number(s.get("SessionName"))),
        )
        for s in rows:
            if len(chosen) >= n:
                return chosen, 0
            chosen.append(s)
    return chosen, n - len(chosen)


def term_no(session) -> int | None:
    """'OV-7062-T5' -> 5."""
    code = (session.get("TermCode") or "")
    tail = code.rsplit("-T", 1)[-1] if "-T" in code else ""
    return int(tail) if tail.isdigit() else None

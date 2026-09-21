"""
One student, every enrollment, one choice.

The portal keeps a separate record per enrollment (course map). A student on
ADSE-DIRECT-FROM-TERM 5 carries an HDSE row too, and an SBTE student carries
an SBTE row. Each has its own term list. Marking must go into the enrollment
that actually contains the term being marked; the audit, however, counts the
student as a whole - distinct dates across ALL their enrollments.

So two views of one student live here:
  pick()          -> the enrollment to WRITE into
  marked_dates()  -> the dates the AUDIT already sees, across everything

Pure except for load(), which is the only function that touches the portal.
"""

import json
from datetime import datetime
from pathlib import Path

from . import planner, portal


def _norm(blob) -> list[dict]:
    """Accept both cache shapes: the old single {"ids","att"} and the new
    {"enrollments":[...]}. Always return a list of {"ids","att"}."""
    if isinstance(blob, dict) and "enrollments" in blob:
        return blob["enrollments"]
    if isinstance(blob, dict) and "att" in blob:
        return [blob]
    return []


def load(p, enrollment_id: str, cache_dir: Path, refresh: bool = False) -> list[dict]:
    """
    All enrollments for a student, each with its attendance record.

    Caches written before 2026-09-21 hold a single enrollment - whichever
    rows[0] happened to be. They still load (offline analysis needs them) but
    a live run always refreshes, which is what brings the other enrollments in.
    """
    f = cache_dir / f"{enrollment_id}.json"
    if not refresh and f.exists():
        try:
            recs = _norm(json.loads(f.read_text(encoding="utf-8")))
            if recs:
                return recs
        except json.JSONDecodeError:
            pass
    if p is None:
        raise RuntimeError("not in cache (re-run without --offline)")

    recs = [{"ids": ids, "att": p.attendance(ids)}
            for ids in p.find_enrollments(enrollment_id)]
    cache_dir.mkdir(exist_ok=True)
    f.write_text(json.dumps({"enrollments": recs}), encoding="utf-8")
    return recs


def save(cache_dir: Path, enrollment_id: str, recs: list[dict]) -> None:
    (cache_dir / f"{enrollment_id}.json").write_text(
        json.dumps({"enrollments": recs}), encoding="utf-8")


def sessions_of(rec) -> list[dict]:
    return portal.unwrap(rec["att"])[2]


def describe(rec) -> str:
    item, batch, _, _ = portal.unwrap(rec["att"])
    terms = sorted({planner.term_no(s) for s in sessions_of(rec)} - {None})
    return (f"{item.get('CourseName')!s:<26} {batch['BatchCode']:<24} "
            f"terms {''.join('T' + str(t) + ' ' for t in terms).strip()}")


def pick(recs: list[dict], term: int | None = None, module: str | None = None,
         cur=None) -> tuple[dict | None, str]:
    """
    Which enrollment to write into.

    Rule (faculty, 2026-09-09): the one that CONTAINS the term being marked.
    Content decides, not the course label - the labels are misleading
    ('ADSE-DIRECT-FROM-TERM 5' is the row that holds T5+T6; 'HDSE' is the one
    that holds T1-T4 and SBTE).

    Ties go to the enrollment with more MARKABLE pending sessions in scope
    (after SBTE and other exclusions), so an SBTE-only T5 never wins over a
    real T5.
    """
    if not recs:
        return None, "no enrollments"
    if len(recs) == 1:
        return recs[0], "only enrollment"

    def scope(s):
        if term is not None:
            return planner.term_no(s) == term
        if module:
            return (s.get("ModuleCode") or "").upper() == module.upper()
        return True

    scored = []
    for r in recs:
        ss = sessions_of(r)
        in_scope = [s for s in ss if scope(s)]
        markable = [s for s in in_scope
                    if not s.get("IsPresent") and not planner.is_excluded(s, cur)]
        scored.append((len(in_scope) > 0, len(markable), len(in_scope), r))

    scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    has, markable, total, best = scored[0]
    what = f"T{term}" if term is not None else (module or "any term")
    if not has:
        return None, f"no enrollment contains {what}"
    return best, (f"{what} found here ({markable} markable of {total}); "
                  f"{len(recs)} enrollments checked")


def marked_dates(recs: list[dict], year: int | None = None,
                 month: int | None = None) -> set[str]:
    """
    Distinct dates already marked, across EVERY enrollment.

    This is the audit's view. The SAR report counts one student, one month,
    distinct days - it does not care which enrollment carried the mark.
    """
    out = set()
    for r in recs:
        for s in sessions_of(r):
            if not (s.get("IsPresent") and s.get("AttendenceDate")):
                continue
            if year and not portal.in_month(s["AttendenceDate"], year, month):
                continue
            out.add(s["AttendenceDate"][:10])
    return out


def marked_dates_by_month(recs: list[dict]) -> dict[tuple[int, int], set[str]]:
    by = {}
    for d in marked_dates(recs):
        dt = datetime.fromisoformat(d)
        by.setdefault((dt.year, dt.month), set()).add(d)
    return by

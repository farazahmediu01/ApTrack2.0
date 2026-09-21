"""
Backfill one student's attendance for a whole book or a whole semester.

This is NOT the monthly flow. mark.py reads the attendance sheet and writes the
delta for one month. This writes EVERY pending session inside a scope you name,
for a student whose record was never filled in - the case that comes up at
semester 5 and 6.

    python backfill.py --student StudentNNNNNNN --term 5
    python backfill.py --student StudentNNNNNNN --module OV-MOD-TABLEAU-21
    python backfill.py --student StudentNNNNNNN --term 5 --commit

    python backfill.py --student StudentNNNNNNN --list      # what scopes exist

Dry run by default; nothing is written without --commit.

DATES
-----
Sessions are dated backwards from today across the batch's real class days,
oldest session getting the oldest date. The weekday pattern (TTS, MWF, ...) is
detected from the student's own recent marking history; override it with
--days if the detection looks wrong.

THE 14/MONTH CAP IS NEVER BROKEN
--------------------------------
A semester is 68-110 sessions, so a naive backfill would put dozens of marks
into one month. Instead each candidate month is filled only up to 14 TOTAL,
counting what the portal already holds for that month, and the walk keeps
stepping further back until every session has a home. A 68-session backfill
therefore lands across roughly five months, ~13 per month - which is what a
real semester looked like anyway.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from src import cpc, enrollment, planner, portal
from reconcile import CACHE, mask_id, mask_name

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / ".cache" / "runs"
DEFAULT_CPC_DIR = ROOT / "data/CPC"

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
PATTERNS = {"TTS": [1, 3, 5], "MWF": [0, 2, 4], "MTWTF": [0, 1, 2, 3, 4]}
RECENT_MARKS = 40          # how far back to look when detecting the pattern
MAX_LOOKBACK_DAYS = 1500   # ~4 years; a guard against an endless walk
MAX_ROUNDS = 10            # retry rounds for locked sessions


# ---------------------------------------------------------------------------
# scope and ordering
# ---------------------------------------------------------------------------

def course_of(sessions) -> str | None:
    """'OV-7062-T5' -> '7062', from whichever term codes are present."""
    for s in sessions:
        parts = (s.get("TermCode") or "").split("-")
        if len(parts) >= 2 and parts[1].isdigit():
            return parts[1]
    return None


def in_scope(s, term, module) -> bool:
    if term is not None:
        return planner.term_no(s) == term
    return (s.get("ModuleCode") or "").upper() == module.upper()


def ordered_pending(sessions, term, module, cur):
    """Pending, in scope, markable - in CPC teaching order."""
    rows = [s for s in sessions
            if not s.get("IsPresent")
            and in_scope(s, term, module)
            and not planner.is_excluded(s, cur)]

    def key(s):
        pos = cur.order_of(s.get("ModuleName")) if cur else None
        return (pos if pos is not None else 10_000,
                s.get("ModuleCode") or "",
                planner.session_number(s.get("SessionName")))

    return sorted(rows, key=key)


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------

def detect_pattern(sessions) -> tuple[list[int], str]:
    """
    Class weekdays, from the student's most recent marks.

    Only recent marks are used: a student who changed batch carries two
    patterns in their full history, and it is the current one we need.
    """
    marked = sorted((s for s in sessions
                     if s.get("IsPresent") and s.get("AttendenceDate")),
                    key=lambda s: s["AttendenceDate"], reverse=True)[:RECENT_MARKS]
    if not marked:
        return PATTERNS["MWF"], "default MWF (no marking history)"
    tally = Counter(datetime.fromisoformat(s["AttendenceDate"][:19]).weekday()
                    for s in marked)
    days = sorted(d for d, n in tally.items() if n >= len(marked) * 0.15)
    if not days:
        return PATTERNS["MWF"], "default MWF (history too sparse)"
    return days, "detected from " + str(len(marked)) + " recent marks"


def pick_dates(n, weekdays, taken: set[str], start=None):
    """
    n class dates walking BACKWARDS from today, oldest first.

    Two rules, both about the audit, which counts DISTINCT DATES per month:
      - a date the student already has a mark on is never reused - a second
        session on it would add nothing;
      - a month is skipped once it holds 14 distinct dates, counting both what
        the portal already has and what this run has planned.
    The walk just keeps going further back until every session has a date.
    """
    per_month = Counter()
    for d in taken:
        y, m = int(d[:4]), int(d[5:7])
        per_month[(y, m)] += 1

    day = start or date.today()
    picked, walked = [], 0
    while len(picked) < n and walked < MAX_LOOKBACK_DAYS:
        key = (day.year, day.month)
        if (day.weekday() in weekdays
                and day.isoformat() not in taken
                and per_month[key] < planner.MONTHLY_CAP):
            per_month[key] += 1
            picked.append(day)
        day -= timedelta(days=1)
        walked += 1

    picked.reverse()          # oldest session gets the oldest date
    return picked


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------

def write(p, rec, recs, student_id, plan, spare, log, rounds=MAX_ROUNDS):
    """
    Write each (date, session), verify by re-read, and for anything that did
    not land keep the DATE and try the next in-scope session on it.

    Same hazard as the monthly flow: a locked session returns HTTP 200 and
    silently does not persist, so the save response proves nothing.
    """
    item, batch_row, _, _ = portal.unwrap(rec["att"])
    landed, locked = set(), []
    for _ in range(rounds):
        attempt = [(d, s) for d, s in plan if d.isoformat() not in landed]
        if not attempt:
            break
        for d, s in attempt:
            code, body = p.save(portal.build_payload(
                item, batch_row, rec["ids"], [s], portal.marked_on(d)))
            if code != 200:
                log.append((s["SessionName"], code, body[:120]))

        rec["att"] = p.attendance(rec["ids"])
        enrollment.save(CACHE, student_id, recs)
        by_id = {x["SessionId"]: x for x in portal.unwrap(rec["att"])[2]}

        for i, (d, s) in enumerate(plan):
            if d.isoformat() in landed:
                continue
            if by_id.get(s["SessionId"], {}).get("IsPresent"):
                landed.add(d.isoformat())
                continue
            if s not in locked:
                locked.append(s)
                log.append((s["SessionName"], "locked", "saved 200 but did not persist"))
            while spare and spare[0] in locked:
                spare.pop(0)
            if spare:
                plan[i] = (d, spare.pop(0))
    return landed, locked


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--student", required=True, help="enrollment id")
    ap.add_argument("--term", type=int, help="semester number, e.g. 5")
    ap.add_argument("--module", help="portal ModuleCode, e.g. OV-MOD-TABLEAU-21")
    ap.add_argument("--days", help="override class days: TTS, MWF, or Mon,Wed,Fri")
    ap.add_argument("--list", action="store_true",
                    help="show this student's enrollments, terms and books, then exit")
    ap.add_argument("--cpc-dir", default=str(DEFAULT_CPC_DIR))
    ap.add_argument("--mask", action="store_true", help="hide id and name")
    ap.add_argument("--commit", action="store_true", help="actually write")
    args = ap.parse_args()

    if not args.list and (args.term is None) == (args.module is None):
        sys.exit("FAIL: give exactly one of --term or --module (or --list to see them)")

    p = portal.Portal()
    left = portal.require_fresh(p.token, need=120)
    print(f"token ok, {left // 60}m {left % 60}s left")

    recs = enrollment.load(p, args.student, CACHE, refresh=True)
    who = args.student if not args.mask else mask_id(args.student)
    first = portal.unwrap(recs[0]["att"])[0]
    name = first["StudentName"] if not args.mask else mask_name(first["StudentName"])
    print(f"{who}  {name}  |  {len(recs)} enrollment(s)")
    for r in recs:
        print(f"    {enrollment.describe(r)}")
    print()

    curricula = cpc.load_all(args.cpc_dir)

    if args.list:
        for r in recs:
            item, batch, sessions, _ = portal.unwrap(r["att"])
            cur = curricula.get(course_of(sessions))
            print(f"== {item.get('CourseName')} / {batch['BatchCode']}")
            print(f"  {'Term':<8}{'Book':<26}{'done':>6}{'pending':>9}")
            seen = defaultdict(lambda: [0, 0])
            for s in sessions:
                if planner.is_excluded(s, cur):
                    continue
                seen[(planner.term_no(s), s["ModuleCode"])][0 if s["IsPresent"] else 1] += 1
            for (t, m), (done, pend) in sorted(seen.items(), key=lambda x: (x[0][0] or 99, x[0][1])):
                print(f"  T{t:<7}{m:<26}{done:>6}{pend:>9}")
            print()
        return

    # ---- which enrollment: the one that actually contains the scope --------
    rec, why = enrollment.pick(recs, term=args.term, module=args.module)
    if rec is None:
        sys.exit(f"FAIL: {why}")
    item, batch_row, sessions, _ = portal.unwrap(rec["att"])
    cur = curricula.get(course_of(sessions))
    print(f"using: {item.get('CourseName')} / {batch_row['BatchCode']}   ({why})")
    if cur is None:
        print(f"! no CPC for course {course_of(sessions)} - falling back to portal order")

    todo = ordered_pending(sessions, args.term, args.module, cur)
    scope = f"T{args.term}" if args.term is not None else args.module
    if not todo:
        sys.exit(f"nothing pending in {scope} (or it is all excluded / already marked)")

    if args.days:
        key = args.days.upper()
        if key in PATTERNS:
            weekdays, how = PATTERNS[key], f"--days {key}"
        else:
            weekdays = sorted(WEEKDAYS.index(d.strip()[:3].title())
                              for d in args.days.split(","))
            how = f"--days {args.days}"
    else:
        weekdays, how = detect_pattern(sessions)

    # ---- dates: never reuse one the student already has, in ANY enrollment --
    taken = enrollment.marked_dates(recs)
    dates = pick_dates(len(todo), weekdays, taken)
    if len(dates) < len(todo):
        sys.exit(f"FAIL: only found {len(dates)} free class dates for {len(todo)} sessions "
                 f"within {MAX_LOOKBACK_DAYS} days")

    print(f"scope {scope}: {len(todo)} pending sessions")
    print(f"class days: {[WEEKDAYS[d] for d in weekdays]}  ({how})")
    by_month = Counter((d.year, d.month) for d in dates)
    have_by_month = Counter((int(x[:4]), int(x[5:7])) for x in taken)
    print("months used (cap 14 distinct dates each, existing counted, no date reused):")
    for (y, m), n in sorted(by_month.items()):
        print(f"  {y}-{m:02d}  {n:>2} new  + {have_by_month[(y, m)]:>2} existing"
              f"  = {n + have_by_month[(y, m)]:>2}")
    print()

    plan = list(zip(dates, todo))
    print(f"  {'Date':<12}{'Session':<28}{'Module'}")
    for d, s in plan:
        print(f"  {d.isoformat():<12}{s['SessionName']:<28}{s['ModuleCode']}")

    if not args.commit:
        print("\nDRY RUN - nothing written. Re-run with --commit to apply.")
        return

    print("\nwriting...")
    refused = []
    landed, locked = write(p, rec, recs, args.student, plan, [], refused)
    # In a backfill the scope IS the point, so a locked session has no
    # substitute: the same date is simply left unfilled and reported.
    print(f"\nwrote {len(landed)} of {len(plan)} dates")
    for n_, c, b in refused:
        print(f"  REFUSED {n_} ({c}) {b}")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log = LOG_DIR / f"backfill-{stamp}.json"
    log.write_text(json.dumps({
        "student": args.student, "scope": scope,
        "enrollment": f"{item.get('CourseName')} / {batch_row['BatchCode']}",
        "written": sorted(landed),
        "locked": [s["SessionName"] for s in locked],
        "dates": [d.isoformat() for d in dates],
    }, indent=2), encoding="utf-8")
    print(f"log: {log}")


if __name__ == "__main__":
    main()

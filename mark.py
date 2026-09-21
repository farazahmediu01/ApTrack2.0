"""
Attendance marker - the only monthly script in this repo that can write.

    python mark.py                      # DRY RUN across every batch
    python mark.py --batch 1            # dry run, one batch
    python mark.py --batch 1 --commit   # actually write batch 1
    python mark.py --commit             # actually write everything

Nothing is written without --commit. A run with no flag is safe to fire at any
time and prints exactly what a real run would do.

THE UNIT IS THE DATE, NOT THE COUNT
-----------------------------------
Management's audit report counts DISTINCT DATES per student per month. Two
sessions on the same date count once. So, per student:

  want  = the class dates the sheet marks P or O
  have  = distinct dates already marked this month, across EVERY enrollment
  todo  = want - have
  for each date in todo: mark one pending session ON THAT DATE

This is idempotent (a second run finds todo empty), self-repairing (a month
with duplicate dates gets its missing dates filled), and it satisfies the
14-day cap by construction - a month never has more than 14 class days.

WHICH ENROLLMENT
----------------
A student may have several. The one containing the term the batch is teaching
receives the writes; the audit view (have) spans all of them.

LOCKED SESSIONS
---------------
A session whose term already has a generated transcript returns HTTP 200 from
the save endpoint and then does not persist. Every write is verified by
re-read; anything that did not land is blacklisted and the SAME DATE is retried
with the next candidate session, so the date - the thing that is audited - is
never lost.
"""

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from src import cpc, enrollment, planner, portal, roster
from reconcile import (CACHE, book_order_from_history, current_term,
                       mask_id, mask_name, target_month)

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / ".cache" / "runs"
DEFAULT_ROSTER = ROOT / "data/Attendence-sheet/Faraz-Ahmed.xlsx"
DEFAULT_CPC_DIR = ROOT / "data/CPC"

MIN_TOKEN_SECONDS = 120
MAX_ROUNDS = 10   # retry rounds for locked sessions; each costs one re-read


def want_dates(student, batch_dates):
    """The class dates this student was P or O on, in calendar order."""
    return [d for code, d in zip(student.marks, batch_dates)
            if code in planner.VALID_CODES]


def candidate_pool(sessions, start_term, cur):
    """Every markable pending session, in fallback order."""
    pending = [s for s in sessions
               if not s.get("IsPresent") and not planner.is_excluded(s, cur)]
    pool, _ = planner.select_sessions(
        sessions, start_term, book_order_from_history(sessions), len(pending), cur)
    return pool


def mark_student(p, sp, st, batch_dates, year, month, term_hint, cur, commit):
    res = {"student": sp.student_id, "name": sp.name, "sheet": sp.want,
           "want_dates": 0, "have": 0, "todo": 0, "written": 0,
           "locked": [], "enrollment": "", "status": ""}

    recs = enrollment.load(p, sp.student_id, CACHE, refresh=commit)
    if not recs:
        res["status"] = "no portal record"
        return res

    want = want_dates(st, batch_dates)
    have = enrollment.marked_dates(recs, year, month)
    todo = [d for d in want if d.isoformat() not in have]
    res.update(want_dates=len(want), have=len(have), todo=len(todo))

    if len(want) > planner.MONTHLY_CAP:
        res["status"] = (f"HOLD: {len(want)} class dates exceeds the "
                         f"{planner.MONTHLY_CAP}/month cap")
        return res
    if not todo:
        res["status"] = "up to date"
        return res

    rec, why = enrollment.pick(recs, term=term_hint, cur=cur)
    if rec is None:
        # No enrollment holds the batch's term - fall back to whichever has
        # the most markable pending anywhere, so the count still gets met.
        rec, why = enrollment.pick(recs, cur=cur)
        if rec is None:
            res["status"] = "HOLD: " + why
            return res
        why = f"no enrollment has T{term_hint}; " + why
    item, batch_row, sessions, _ = portal.unwrap(rec["att"])
    res["enrollment"] = f"{item.get('CourseName')} / {batch_row['BatchCode']}"
    res["why"] = why

    term_now, _ = current_term(sessions, term_hint)
    pool = candidate_pool(sessions, term_now, cur)
    if not pool:
        res["status"] = f"SHORT {len(todo)} - no pending sessions outside SBTE"
        return res

    # Pair dates with sessions. The date is fixed; the session is what gets
    # swapped if the portal refuses it.
    plan = list(zip(todo, pool))
    spare = pool[len(todo):]
    res["plan"] = [{"date": d.isoformat(), "SessionId": s["SessionId"],
                    "SessionName": s["SessionName"], "ModuleCode": s["ModuleCode"],
                    "TermCode": s["TermCode"]} for d, s in plan]
    if len(plan) < len(todo):
        res["status"] = f"SHORT {len(todo) - len(plan)} - only {len(pool)} pending available"

    if not commit:
        res["status"] = res["status"] or f"would mark {len(plan)} dates"
        return res

    landed, locked, rounds = set(), [], 0
    while len(landed) < len(plan) and rounds < MAX_ROUNDS:
        rounds += 1
        attempt = [(d, s) for d, s in plan if d.isoformat() not in landed]
        if not attempt:
            break
        for d, s in attempt:
            code, _ = p.save(portal.build_payload(
                item, batch_row, rec["ids"], [s], portal.marked_on(d)))
            if code != 200 and s not in locked:
                locked.append(s)

        after = p.attendance(rec["ids"])
        rec["att"] = after
        enrollment.save(CACHE, sp.student_id, recs)
        _, _, now, _ = portal.unwrap(after)
        by_id = {x["SessionId"]: x for x in now}

        for i, (d, s) in enumerate(plan):
            if d.isoformat() in landed:
                continue
            if by_id.get(s["SessionId"], {}).get("IsPresent"):
                landed.add(d.isoformat())
                continue
            if s not in locked:
                locked.append(s)
            while spare and spare[0] in locked:   # same date, next candidate
                spare.pop(0)
            if spare:
                plan[i] = (d, spare.pop(0))

    res["written"] = len(landed)
    res["locked"] = [s["SessionName"] for s in locked]
    if len(landed) < len(plan):
        res["status"] = (f"marked {len(landed)} of {len(plan)} dates - "
                         f"{len(locked)} locked, no replacement left")
    else:
        res["status"] = f"marked {len(landed)} dates"
        if locked:
            res["status"] += f" ({len(locked)} locked, replaced)"
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--roster", default=str(DEFAULT_ROSTER))
    ap.add_argument("--cpc-dir", default=str(DEFAULT_CPC_DIR))
    ap.add_argument("--batch", type=int)
    ap.add_argument("--limit", type=int, help="only the first N students per batch")
    ap.add_argument("--mask", action="store_true", help="hide ids and names")
    ap.add_argument("--commit", action="store_true", help="actually write (default: dry run)")
    ap.add_argument("--offline", action="store_true", help="dry run from cache only")
    args = ap.parse_args()

    if args.commit:
        p = portal.Portal()
        left = portal.require_fresh(p.token, need=MIN_TOKEN_SECONDS)
        print(f"token ok, {left // 60}m {left % 60}s left")
    else:
        p = None if args.offline else portal.Portal()
        if p is not None and (portal.token_seconds_left(p.token) or 0) < 60:
            print("token stale - dry run will use cached records")
            p = None
    print("MODE: " + ("COMMIT - this will write to the portal" if args.commit
                      else "DRY RUN - nothing will be written"))
    print()

    curricula = cpc.load_all(args.cpc_dir)
    batches = roster.load(args.roster)
    plans = planner.plan_all(batches, curricula)
    idx = list(range(len(plans))) if not args.batch else [args.batch - 1]

    tally = Counter()
    results = []
    for i in idx:
        b, pl = batches[i], plans[i]
        year, month = target_month(b)
        cur = curricula.get(pl.course)
        print("=" * 96)
        print(f"BATCH {i + 1}  {pl.batch_code}   target {year}-{month:02d}   "
              f"term T{pl.start_term or '?'}")
        print("=" * 96)
        print(f"  {'Student':<15} {'Name':<22} {'want':>5} {'have':>5} {'todo':>5}  status")
        print("  " + "-" * 92)

        students = pl.students[:args.limit] if args.limit else pl.students
        for sp, st in zip(students, b.students):
            sid = sp.student_id if not args.mask else mask_id(sp.student_id)
            nm = (sp.name if not args.mask else mask_name(sp.name))[:22]
            try:
                r = mark_student(p, sp, st, b.dates, year, month,
                                 pl.start_term, cur, args.commit)
            except Exception as e:
                print(f"  {sid:<15} {nm:<22} ERROR: {e}")
                tally["error"] += 1
                results.append({"student": sp.student_id, "error": str(e)})
                continue
            results.append(r)
            tally["written"] += r["written"]
            tally["planned"] += len(r.get("plan", []))
            tally["locked"] += len(r["locked"])
            if r["status"].startswith("HOLD"):
                tally["held"] += 1
            print(f"  {sid:<15} {nm:<22} {r['want_dates']:>5} {r['have']:>5} "
                  f"{r['todo']:>5}  {r['status']}")
            if "enrollments checked" in r.get("why", ""):
                print(f"      enrollment: {r['enrollment']}  ({r['why']})")
            for name in r["locked"]:
                print(f"      REFUSED {name} (locked)")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log = LOG_DIR / f"{'commit' if args.commit else 'dryrun'}-{stamp}.json"
    log.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    print("\n" + "=" * 96)
    if args.commit:
        print(f"WROTE {tally['written']} dates of {tally['planned']} planned | "
              f"{tally['locked']} sessions refused | {tally['held']} held | "
              f"{tally['error']} errors")
    else:
        print(f"WOULD WRITE {tally['planned']} dates | {tally['held']} held | "
              f"{tally['error']} errors")
        print("Re-run with --commit to apply.")
    print(f"log: {log}")
    print("=" * 96)


if __name__ == "__main__":
    main()

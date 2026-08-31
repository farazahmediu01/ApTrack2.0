"""
Attendance marker - the only script in this repo that can write.

    python mark.py                      # DRY RUN across every batch
    python mark.py --batch 1            # dry run, one batch
    python mark.py --batch 1 --commit   # actually write batch 1
    python mark.py --commit             # actually write everything

Nothing is written without --commit. A run with no flag is safe to fire at any
time and prints exactly what a real run would do.

How it works, per student:
  1. read current portal state
  2. already = sessions marked with a date inside the target month
  3. delta   = min(sheet count, 14) - already        <- the idempotency guard
  4. pick `delta` pending sessions: current term first, then earlier terms
     ascending, then later ones; SBTE is never touched
  5. write them one at a time, each dated with a real class date the student
     was present on
  6. re-read and verify. The save endpoint returns HTTP 200 for a LOCKED
     session (one whose term already has a generated transcript) and then
     silently does not persist it, so the response cannot be trusted. Anything
     that did not land is blacklisted and replaced with the next candidate, so
     the student still reaches their count.

Re-running is safe. Step 3 measures what is already there, so a second run
marks nothing. It is also the resume mechanism if a token expires mid-run.
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from src import cpc, planner, portal, roster
from reconcile import (CACHE, book_order_from_history, cached, current_term,
                       fetch, mask_id, mask_name, prefetch, target_month)

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / ".cache" / "runs"
DEFAULT_ROSTER = ROOT / "data/Attendence-sheet/Faraz-Ahmed.xlsx"
DEFAULT_CPC_DIR = ROOT / "data/CPC"

MIN_TOKEN_SECONDS = 120
MAX_REPLACEMENT_ROUNDS = 10  # locked sessions get replaced, but not forever


def valid_dates(student, dates):
    """The class dates this student was P or O on, in calendar order."""
    return [d for code, d in zip(student.marks, dates)
            if code in planner.VALID_CODES]


def mark_student(p, sp, st, batch_dates, year, month, commit, cur=None):
    """
    Returns a result dict. Performs writes only when commit is True.
    """
    blob = cached(sp.student_id) if p is None else fetch(p, sp.student_id)
    if blob is None:
        raise RuntimeError("not in cache")
    item, batch_row, sessions, _ = portal.unwrap(blob["att"])

    already = sum(1 for s in sessions
                  if s.get("IsPresent") and portal.in_month(
                      s.get("AttendenceDate"), year, month))
    delta = max(0, sp.capped - already)

    res = {"student": sp.student_id, "name": sp.name, "sheet": sp.want,
           "capped": sp.capped, "already": already, "delta": delta,
           "written": 0, "refused": [], "status": ""}

    if delta == 0:
        res["status"] = "up to date"
        return res

    term_now, _ = current_term(sessions, None)
    chosen, short = planner.select_sessions(
        sessions, term_now, book_order_from_history(sessions), delta, cur)

    if not chosen:
        res["status"] = f"SHORT {delta} - no pending sessions outside SBTE"
        return res
    if short:
        res["status"] = f"SHORT {short} - only {len(chosen)} pending available"

    # Date each session with a real class date the student was present on.
    # The earlier marks are assumed to be the ones already recorded, so take
    # the trailing `len(chosen)` dates. Every one of them is inside the target
    # month by construction, which is what management audits.
    days = valid_dates(st, batch_dates)[-len(chosen):]
    while len(days) < len(chosen):                    # defensive; should not happen
        days.append(days[-1] if days else batch_dates[-1])

    res["plan"] = [{"SessionId": s["SessionId"], "SessionName": s["SessionName"],
                    "ModuleCode": s["ModuleCode"], "TermCode": s["TermCode"],
                    "date": d.isoformat()} for s, d in zip(chosen, days)]

    if not commit:
        res["status"] = res["status"] or f"would mark {len(chosen)}"
        return res

    # Write, verify, and REPLACE anything that did not stick.
    #
    # A locked session - one whose term already has a generated transcript -
    # returns HTTP 200 from the save endpoint and then simply does not persist.
    # The response cannot be trusted; only a re-read can tell. So: write the
    # batch, re-read, and for every session that failed to land, blacklist it
    # and pick the NEXT candidate instead. Otherwise the student silently ends
    # up short of the count management audits.
    locked, written, rounds = [], [], 0
    while len(written) < delta and rounds < MAX_REPLACEMENT_ROUNDS:
        rounds += 1
        todo = [s for s in chosen if s not in written and s not in locked]
        if not todo:
            break

        for s in todo:
            day = days[min(chosen.index(s), len(days) - 1)]
            code, body = p.save(portal.build_payload(
                item, batch_row, blob["ids"], [s], portal.marked_on(day)))
            if code != 200:
                locked.append(s)
                res["refused"].append((s["SessionName"], code, body[:120]))

        after = p.attendance(blob["ids"])
        _, _, now_sessions, _ = portal.unwrap(after)
        (CACHE / f"{sp.student_id}.json").write_text(
            json.dumps({"ids": blob["ids"], "att": after}), encoding="utf-8")

        by_id = {s["SessionId"]: s for s in now_sessions}
        for s in todo:
            if by_id.get(s["SessionId"], {}).get("IsPresent"):
                written.append(s)
            elif s not in locked:
                locked.append(s)
                res["refused"].append((s["SessionName"], "locked", "saved 200 but did not persist"))

        if len(written) < delta:
            # Re-select from the refreshed record, skipping everything locked.
            fresh = [s for s in now_sessions
                     if s["SessionId"] not in {x["SessionId"] for x in locked}]
            more, _ = planner.select_sessions(
                fresh, term_now, book_order_from_history(now_sessions),
                delta - len(written), cur)
            chosen = written + more
            days = valid_dates(st, batch_dates)[-len(chosen):] or days

    res["written"] = len(written)
    res["locked"] = [s["SessionName"] for s in locked]
    if len(written) < delta:
        res["status"] = (f"marked {len(written)} of {delta} - "
                         f"{len(locked)} locked, no replacement available")
    else:
        res["status"] = f"marked {len(written)}"
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
    ap.add_argument("--mask", action="store_true",
                    help="hide student ids and names (for sharing output)")
    ap.add_argument("--commit", action="store_true",
                    help="actually write (default: dry run)")
    ap.add_argument("--offline", action="store_true",
                    help="dry run from cached records only")
    args = ap.parse_args()

    # A dry run may read from cache; a commit must not. Writing against a stale
    # read could double-mark, so --commit always refetches first.
    if args.commit:
        p = portal.Portal()
        left = portal.require_fresh(p.token, need=MIN_TOKEN_SECONDS)
        print(f"token ok, {left // 60}m {left % 60}s left")
    else:
        p = portal.Portal() if not args.offline else None
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

    wanted = [sp.student_id for i in idx
              for sp in (plans[i].students[:args.limit] if args.limit
                         else plans[i].students)
              if not sp.blockers]
    if p is not None:
        prefetch(p, wanted, refresh=args.commit)

    tally = Counter()
    results = []

    for i in idx:
        b, pl = batches[i], plans[i]
        year, month = target_month(b)
        print("=" * 90)
        print(f"BATCH {i + 1}  {pl.batch_code}   target {year}-{month:02d}")
        print("=" * 90)

        students = pl.students[:args.limit] if args.limit else pl.students
        for sp, st in zip(students, b.students):
            sid = sp.student_id if (not args.mask) else mask_id(sp.student_id)
            nm = (sp.name if (not args.mask) else mask_name(sp.name))[:22]

            if sp.blockers:
                print(f"  {sid:<15} {nm:<22} HOLD: {sp.blockers[0][:44]}")
                tally["held"] += 1
                continue

            try:
                cur = curricula.get(pl.course)
                r = mark_student(p, sp, st, b.dates, year, month, args.commit, cur)
            except Exception as e:
                print(f"  {sid:<15} {nm:<22} ERROR: {e}")
                tally["error"] += 1
                results.append({"student": sp.student_id, "error": str(e)})
                continue

            results.append(r)
            tally["written"] += r["written"]
            tally["planned"] += len(r.get("plan", []))
            if r["refused"]:
                tally["refused"] += len(r["refused"])
            print(f"  {sid:<15} {nm:<22} sheet {r['sheet']:>2}  portal {r['already']:>2}  "
                  f"delta {r['delta']:>2}  -> {r['status']}")
            for name, code, body in r["refused"]:
                print(f"      REFUSED {name} ({code}) {body}")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log = LOG_DIR / f"{'commit' if args.commit else 'dryrun'}-{stamp}.json"
    log.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    print("\n" + "=" * 90)
    if args.commit:
        print(f"WROTE {tally['written']} of {tally['planned']} planned | "
              f"{tally['refused']} refused | {tally['held']} held | "
              f"{tally['error']} errors")
    else:
        print(f"WOULD WRITE {tally['planned']} sessions | {tally['held']} held | "
              f"{tally['error']} errors")
        print("Re-run with --commit to apply.")
    print(f"log: {log}")
    print("=" * 90)


if __name__ == "__main__":
    main()

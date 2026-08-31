"""
Portal reconciliation - READ ONLY.

For every student in the roster: read what the portal already has, compare it
against what the sheet says, and report the delta that still needs marking.

    python reconcile.py                     # all batches
    python reconcile.py --batch 6           # one batch
    python reconcile.py --names             # unmask ids and names
    python reconcile.py --refresh           # ignore the on-disk cache

This script has NO write path. It cannot mark attendance. Responses are cached
under .cache/ (gitignored - they contain student records) so a re-run inside a
token window costs nothing.

Needs a fresh ACCESS_TOKEN in .env. Gateway tokens live ~15 minutes:
DevTools Console -> sessionStorage.token
"""

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from src import cpc, planner, portal, roster

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / ".cache"
DEFAULT_ROSTER = ROOT / "data/Attendence-sheet/Faraz-Ahmed.xlsx"
DEFAULT_CPC_DIR = ROOT / "data/CPC"

ENROLL_RE = re.compile(r"\b[A-Za-z]*\d{6,}\b")


def mask_name(v):
    return " ".join(p[0] + "*" * (len(p) - 1) for p in (v or "").split() if p) or "?"


def mask_id(v):
    return ENROLL_RE.sub(lambda m: m.group(0)[:2] + "*" * (len(m.group(0)) - 2), v or "")


def target_month(batch):
    """(year, month) from the batch's own date columns - the most reliable source."""
    if not batch.dates:
        return None, None
    ym = Counter((d.year, d.month) for d in batch.dates).most_common(1)[0][0]
    return ym


def book_order_from_history(sessions):
    """
    Teaching order of modules, learned from the student's own record.

    The portal's array order is NOT teaching order - modules come back roughly
    alphabetically and session numbers inside them are shuffled. But the dates
    on already-marked sessions are ground truth: whichever module was being
    taught first was marked first.

    Modules never taught have no date and sort last, by code, deterministically.
    """
    first = {}
    for s in sessions:
        if not s.get("IsPresent"):
            continue
        d = s.get("AttendenceDate")
        if not d:
            continue
        mc = s.get("ModuleCode") or ""
        if mc not in first or d < first[mc]:
            first[mc] = d
    codes = {s.get("ModuleCode") or "" for s in sessions}
    return sorted(codes, key=lambda c: (first.get(c) is None, first.get(c, ""), c))


def current_term(sessions, fallback):
    """
    The student's own current term, from their most recently marked session.

    The sheet's Module row gives the term the BATCH is teaching, which is right
    for most students and wrong for the rest: several students sitting in these
    batches are on a 5-term structure (72/79/80/72/104, no T6) or on an SBTE
    batch entirely. Telling them to start at T6 sends the fallback straight to
    T1, skipping the term they are actually in.

    Per-student beats per-batch. Fall back to the batch's term only when the
    student has no marking history at all.
    """
    marked = [s for s in sessions if s.get("IsPresent") and s.get("AttendenceDate")
              and not planner.is_excluded(s)]
    if not marked:
        return fallback, "batch"
    latest = max(marked, key=lambda s: s["AttendenceDate"])
    return (planner.term_no(latest) or fallback), "history"


def cached(enrollment_id):
    f = CACHE / f"{enrollment_id}.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None


def fetch(p, enrollment_id, refresh=False):
    CACHE.mkdir(exist_ok=True)
    if not refresh:
        hit = cached(enrollment_id)
        if hit:
            return hit
    ids = p.find_student(enrollment_id)
    att = p.attendance(ids)
    blob = {"ids": ids, "att": att}
    (CACHE / f"{enrollment_id}.json").write_text(json.dumps(blob), encoding="utf-8")
    return blob


def prefetch(p, ids, refresh, workers=8):
    """
    Pull every student's record up front, concurrently.

    Sequentially this is ~200 requests against a token that lives 15 minutes -
    uncomfortably close. In parallel it is well under a minute, and whatever
    lands stays in the cache, so a token that dies mid-run only costs the
    students that had not been fetched yet.
    """
    from concurrent.futures import ThreadPoolExecutor

    todo = [s for s in ids if refresh or not cached(s)]
    if not todo:
        print(f"all {len(ids)} students already cached\n")
        return {}
    print(f"fetching {len(todo)} of {len(ids)} students ({workers} at a time)...")
    errors, done = {}, 0

    def one(sid):
        try:
            fetch(p, sid, refresh)
            return sid, None
        except Exception as e:
            return sid, e

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for sid, err in ex.map(one, todo):
            done += 1
            if err:
                errors[sid] = err
            if done % 20 == 0 or done == len(todo):
                left = portal.token_seconds_left(p.token) or 0
                print(f"  {done}/{len(todo)}  (token {left // 60}m {left % 60}s left)")
    if errors:
        print(f"  {len(errors)} failed; they will show as ERR below")
    print()
    return errors


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--roster", default=str(DEFAULT_ROSTER))
    ap.add_argument("--cpc-dir", default=str(DEFAULT_CPC_DIR))
    ap.add_argument("--batch", type=int)
    ap.add_argument("--mask", action="store_true",
                    help="hide student ids and names (for sharing output)")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache")
    ap.add_argument("--limit", type=int, help="only the first N students per batch")
    ap.add_argument("--offline", action="store_true",
                    help="use cached records only; no token needed")
    args = ap.parse_args()

    # Analysis needs no network once the records are cached, and the cache
    # outlives the 15-minute token by a long way. --offline lets a re-run happen
    # at any time; only fetching students not yet cached needs a live token.
    if args.offline:
        p = None
        print("offline: using cached records only\n")
    else:
        p = portal.Portal()
        left = portal.require_fresh(p.token)
        print(f"token ok, {left // 60}m {left % 60}s left\n")

    curricula = cpc.load_all(args.cpc_dir)
    batches = roster.load(args.roster)
    plans = planner.plan_all(batches, curricula)
    idx = list(range(len(plans))) if not args.batch else [args.batch - 1]

    wanted = [sp.student_id
              for i in idx
              for sp in (plans[i].students[:args.limit] if args.limit
                         else plans[i].students)
              if not sp.blockers]
    if p is not None:
        prefetch(p, wanted, args.refresh)

    grand = Counter()
    problems = []

    for i in idx:
        b, pl = batches[i], plans[i]
        year, month = target_month(b)
        print("=" * 92)
        print(f"BATCH {i + 1}  {pl.batch_code}   course {pl.course or '(none)'}   "
              f"{pl.days} {pl.time}   target {year}-{month:02d}")
        if pl.start_term:
            print(f"  starting term T{pl.start_term} (from {pl.start_term_source}); "
                  f"books {', '.join(pl.books)}")
        for n in pl.notes:
            print(f"  ! {n}")
        print("=" * 92)
        print(f"  {'Student':<15} {'Name':<22} {'sheet':>5} {'portal':>6} {'delta':>5} "
              f"{'avail':>5}  {'plan'}")
        print("  " + "-" * 88)

        students = pl.students[:args.limit] if args.limit else pl.students
        for sp in students:
            sid = sp.student_id if (not args.mask) else mask_id(sp.student_id)
            nm = (sp.name if (not args.mask) else mask_name(sp.name))[:22]

            if sp.blockers:
                print(f"  {sid:<15} {nm:<22} {sp.want:>5} {'-':>6} {'-':>5} {'-':>5}  "
                      f"HOLD: {sp.blockers[0][:34]}")
                problems.append((sp.student_id, sp.blockers[0]))
                grand["hold"] += 1
                continue

            try:
                blob = cached(sp.student_id) if p is None else fetch(p, sp.student_id)
                if blob is None:
                    raise RuntimeError("not in cache (re-run without --offline)")
            except Exception as e:
                print(f"  {sid:<15} {nm:<22} {sp.want:>5} {'ERR':>6} {'-':>5} {'-':>5}  {e}")
                problems.append((sp.student_id, f"portal read failed: {e}"))
                grand["error"] += 1
                continue

            item, batch_row, sessions, terms = portal.unwrap(blob["att"])

            tag = planner.enrollment_note(item)
            if tag:
                grand["oddstatus"] += 1

            already = sum(1 for s in sessions
                          if s.get("IsPresent") and portal.in_month(
                              s.get("AttendenceDate"), year, month))
            cur = curricula.get(pl.course)
            avail = sum(1 for s in sessions
                        if not s.get("IsPresent") and not planner.is_excluded(s, cur))
            sbte = sum(1 for s in sessions
                       if not s.get("IsPresent") and planner.is_excluded(s))
            delta = max(0, sp.capped - already)

            term_now, term_src = current_term(sessions, pl.start_term)
            chosen, short = planner.select_sessions(
                sessions, term_now, book_order_from_history(sessions), delta, cur)
            if term_src == "history" and pl.start_term and term_now != pl.start_term:
                grand["offterm"] += 1

            note = ""
            if already > sp.capped:
                note = f"OVER: portal has {already}, sheet says {sp.capped}"
                problems.append((sp.student_id, note))
                grand["over"] += 1
            elif delta == 0:
                note = "up to date"
                grand["uptodate"] += 1
            elif short:
                note = f"SHORT {short} - only {avail} pending across all terms"
                problems.append((sp.student_id, note))
                grand["short"] += 1
            else:
                spread = sorted({planner.term_no(s) for s in chosen} - {None})
                note = f"T{'+T'.join(map(str, spread))}" if spread else "no term"
                if term_src == "history" and pl.start_term and term_now != pl.start_term:
                    note += f"  (student is in T{term_now}, batch teaches T{pl.start_term})"
                grand["ok"] += 1
                grand["to_mark"] += len(chosen)

            print(f"  {sid:<15} {nm:<22} {sp.want:>5} {already:>6} {delta:>5} "
                  f"{avail:>5}  {note}" + (f"  [{tag}]" if tag else ""))

        print()

    print("=" * 92)
    print(f"SUMMARY  {grand['to_mark']} sessions to mark | {grand['ok']} students ready | "
          f"{grand['uptodate']} already up to date | {grand['hold']} held | "
          f"{grand['short']} short | {grand['oddstatus']} with odd enrollment status | "
          f"{grand['over']} over-marked | {grand['error']} errors | "
          f"{grand['offterm']} not in the batch's term")
    if problems:
        print(f"\n{len(problems)} student(s) need attention:")
        for sid, why in problems:
            print(f"  {sid if (not args.mask) else mask_id(sid)}: {why}")
    print("\nREAD ONLY - nothing was written.")
    print("=" * 92)


if __name__ == "__main__":
    main()

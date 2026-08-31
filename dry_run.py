"""
Offline roster preview - no portal, no network.

Reads the roster and the CPCs and prints what each student's sheet count is,
which term the batch is teaching, and who is held by the 14/month cap.

    python dry_run.py
    python dry_run.py --roster data/Attendence-sheet/Faraz-Ahmed.xlsx
    python dry_run.py --names        # unmask ids and names
    python dry_run.py --batch 6

This is the sheet side only. It cannot tell you what still needs marking -
that needs portal state, which is what reconcile.py does.

Names and enrollment ids are masked by default; the repository is public.
"""

import argparse
import re
import sys
from pathlib import Path

from src import cpc, planner, roster

ROOT = Path(__file__).resolve().parent
DEFAULT_ROSTER = ROOT / "data/Attendence-sheet/Faraz-Ahmed.xlsx"
DEFAULT_CPC_DIR = ROOT / "data/CPC"

ENROLL_RE = re.compile(r"\b[A-Za-z]*\d{6,}\b")


def mask_name(v):
    return " ".join(p[0] + "*" * (len(p) - 1) for p in (v or "").split() if p) or "?"


def mask_id(v):
    return ENROLL_RE.sub(lambda m: m.group(0)[:2] + "*" * (len(m.group(0)) - 2), v or "")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--roster", default=str(DEFAULT_ROSTER))
    ap.add_argument("--cpc-dir", default=str(DEFAULT_CPC_DIR))
    ap.add_argument("--mask", action="store_true",
                    help="hide student ids and names (for sharing output)")
    ap.add_argument("--batch", type=int)
    args = ap.parse_args()

    for p in (Path(args.roster), Path(args.cpc_dir)):
        if not p.exists():
            sys.exit(f"FAIL: not found - {p}")

    curricula = cpc.load_all(args.cpc_dir)
    print(f"curricula: {sorted(curricula)}")
    for code, cur in sorted(curricula.items()):
        print("  " + code + "  " + ", ".join(
            f"T{i}:{s.lo}-{s.hi}" for i, s in enumerate(cur.semesters, 1)))

    batches = roster.load(args.roster)
    plans = planner.plan_all(batches, curricula)
    idx = range(len(plans)) if not args.batch else [args.batch - 1]

    total = held = 0
    for i in idx:
        b, pl = batches[i], plans[i]
        print("\n" + "=" * 84)
        print(f"BATCH {i + 1}  {pl.batch_code}   course {pl.course or '(none)'}   "
              f"{pl.days} {pl.time}   month {pl.month}")
        span = f"{b.dates[0]} .. {b.dates[-1]}" if b.dates else "no dates"
        print(f"  start {b.start_date} | {len(b.dates)} class dates ({span})")
        if pl.start_term:
            print(f"  starting term T{pl.start_term} (from {pl.start_term_source})")
        print(f"  books: {', '.join(pl.books) or '(none)'}")
        for n in pl.notes:
            print(f"  ! {n}")
        print("=" * 84)

        print(f"  {'Student':<15} {'Name':<26} {'P+O':>4} {'will try':>9}")
        print("  " + "-" * 60)
        for sp in pl.students:
            sid = sp.student_id if (not args.mask) else mask_id(sp.student_id)
            nm = (sp.name if (not args.mask) else mask_name(sp.name))[:26]
            flag = f"HOLD ({sp.blockers[0][:28]})" if sp.blockers else str(sp.capped)
            print(f"  {sid:<15} {nm:<26} {sp.want:>4} {flag:>9}")

        n_hold = sum(1 for s in pl.students if s.blockers)
        zero = sum(1 for s in pl.students if not s.blockers and s.capped == 0)
        total += pl.total_wanted
        held += n_hold
        print("  " + "-" * 60)
        print(f"  {len(pl.students)} students | {pl.total_wanted} sessions wanted | "
              f"{zero} with nothing to mark | {n_hold} held")

    print("\n" + "=" * 84)
    print(f"TOTAL {total} sessions wanted across {len(list(idx))} batches | {held} held")
    print("Sheet side only. Run reconcile.py for the portal delta. Nothing was written.")
    print("=" * 84)


if __name__ == "__main__":
    main()

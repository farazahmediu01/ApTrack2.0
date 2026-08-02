"""
SPIKE 01 - Can we mark attendance from Python, with no browser?

Throwaway code. It answers three questions and nothing else:
  1. Does a plain HTTP client with a Bearer token get past the gateway?
  2. Does the save endpoint accept a write from outside the SPA?
  3. Does the write actually stick (verified by re-reading)?

Dry-run by default. Nothing is written unless you pass --commit.

    python spikes/01_mark_one_session.py --student StudentNNNNNNN \
        --module OV-EP-HADOOP-21 --session 1              # look, don't touch
    ... --commit                                          # actually mark it

The token is read from .env and is never printed. No student identifier is
hardcoded here - this file is committed to a repository with a remote, and
student names and enrollment IDs must not go into it.
"""

import argparse
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://aptrackglobal.com/apigateway/api"

CENTRE_MAP_ID = 4985  # not personal data - identifies the centre, not a student


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--student", required=True, help="enrollment id, e.g. StudentNNNNNNN")
    p.add_argument("--module", required=True, help="portal ModuleCode, e.g. OV-EP-HADOOP-21")
    p.add_argument("--session", required=True, type=int, help="session number within the module")
    p.add_argument("--commit", action="store_true", help="actually write (default: dry run)")
    return p.parse_args()


def check_not_expired(token: str) -> None:
    """
    The gateway token lives ~15 MINUTES, not 30 days. Fail loudly and early
    rather than letting the user puzzle over a bare 401.
    """
    import base64

    raw = token[7:] if token.lower().startswith("bearer ") else token
    parts = raw.split(".")
    if len(parts) != 3:
        print("  WARN: token is not JWT-shaped; skipping expiry check")
        return
    body = parts[1] + "=" * (-len(parts[1]) % 4)
    exp = json.loads(base64.urlsafe_b64decode(body)).get("exp")
    if not exp:
        return
    left = exp - int(time.time())
    if left <= 0:
        sys.exit(
            f"FAIL: ACCESS_TOKEN expired {abs(left) // 60} min ago.\n"
            "  These tokens are short-lived. Copy a fresh 'authorization' header\n"
            "  from DevTools into .env and re-run straight away."
        )
    print(f"  token valid for another {left // 60}m {left % 60}s")


def load_token() -> str:
    """Read ACCESS_TOKEN from .env. Never log the value."""
    env = ROOT / ".env"
    if not env.exists():
        sys.exit("FAIL: no .env file at project root")
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "ACCESS_TOKEN":
            token = value.strip().strip("'\"")
            if not token:
                sys.exit("FAIL: ACCESS_TOKEN is empty")
            # tolerate the value being pasted with or without the prefix
            token = token if token.lower().startswith("bearer ") else f"Bearer {token}"
            check_not_expired(token)
            return token
    sys.exit("FAIL: ACCESS_TOKEN not found in .env")


def session_for(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "authority": "aptrackglobal.com",
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "alllevel1values": "",
        "apt2_tz": "Asia/Karachi",
        "authorization": token,
        "content-type": "application/json",
        "origin": "https://aptrackglobal.com",
        "referer": "https://aptrackglobal.com/aptrack/",
    })
    return s


def marked_on_date_noon_karachi() -> str:
    """
    Pin the timestamp to 12:00 Karachi -> 07:00Z.

    The portal sends 'now' stapled onto the picked date, so a mark made at
    01:00 Karachi becomes the PREVIOUS day in UTC (confirmed: picked 30 Jul,
    stored 29 Jul). At noon local, the UTC and local calendar dates always
    agree, so the stored date is the date we meant.
    """
    return f"{date.today().isoformat()}T07:00:00.000Z"


def session_number(name: str) -> int:
    """SessionName 'HADOOP-21_Session07' -> 7. Sorting on the string is wrong."""
    m = re.search(r"(\d+)\s*$", name or "")
    return int(m.group(1)) if m else -1


def get(s, path, **params):
    r = s.get(f"{BASE}/{path}", params=params, timeout=60)
    print(f"  GET {path} -> HTTP {r.status_code}")
    r.raise_for_status()
    return r.json()


def main() -> None:
    args = parse_args()
    commit = args.commit
    s = session_for(load_token())

    print("\n[1] resolve the student's internal IDs")
    found = get(s, "batchmanagement/GetCentreWiseStudentFilter",
                centreMapId=CENTRE_MAP_ID, type=1, searchValue=args.student)
    (ROOT / "tests/fixtures/filter.json").write_text(
        json.dumps(found, indent=2), encoding="utf-8")

    # this endpoint returns a BARE LIST, unlike the others which wrap in {"Item": ...}
    rows = found if isinstance(found, list) else (found.get("Item") or found.get("Items") or [])
    if isinstance(rows, dict):
        rows = [rows]
    if not rows:
        sys.exit(f"FAIL: no student matched {args.student}\n{json.dumps(found)[:800]}")
    row = rows[0]
    print(f"  matched: {json.dumps(row)[:300]}")

    def pick(*names):
        for n in names:
            for k, v in row.items():
                if k.lower() == n.lower():
                    return v
        sys.exit(f"FAIL: none of {names} in filter response. keys={list(row)}")

    # NOTE the read/write naming mismatch: the filter calls them RegularBatchId
    # and CourseMapId; every other endpoint calls them BatchId and
    # StudentCourseMapId. Same values, different names.
    ids = {
        "studentDetailId": pick("StudentDetailId"),
        "studentCourseMapId": pick("CourseMapId", "StudentCourseMapId"),
        "courseId": pick("CourseId"),
        "batchId": pick("RegularBatchId", "BatchId"),
    }
    print(f"  ids: {ids}")

    print("\n[2] read the full attendance state")
    att = get(s, "batchmanagement/GetCentreWiseStudentMarkAttendance",
              IsLoginRole="FAC", **ids)
    (ROOT / "tests/fixtures/markattendance_before.json").write_text(
        json.dumps(att, indent=2), encoding="utf-8")

    item = att["Item"]
    batch = item["BatchDetails"][0]
    sessions = batch["SessionDetails"]
    print(f"  {item['StudentName']} | batch {batch['BatchCode']} | "
          f"{len(sessions)} sessions | {sum(not x['IsPresent'] for x in sessions)} pending")

    print(f"\n[3] locate {args.module} session {args.session}")
    in_module = [x for x in sessions if x["ModuleCode"] == args.module]
    if not in_module:
        codes = sorted({x["ModuleCode"] for x in sessions})
        sys.exit(f"FAIL: {args.module} not in this student's course.\n  available: {codes}")

    target = next((x for x in in_module
                   if session_number(x["SessionName"]) == args.session), None)
    if target is None:
        sys.exit("FAIL: session number not found. module has: "
                 + ", ".join(x["SessionName"] for x in in_module))

    print(f"  {target['SessionName']}  ({target['ModuleName']}, {target['TermName']})")
    print(f"  SessionId={target['SessionId']}  IsPresent={target['IsPresent']}  "
          f"AttendenceDate={target['AttendenceDate']}")

    if target["IsPresent"]:
        sys.exit("STOP: already marked. Pick a pending session or unmark this one first.")

    payload = {
        "BatchId": batch["BatchId"],
        "IsRegularBatch": True,
        "StudentDetailId": ids["studentDetailId"],
        "StudentId": item["StudentId"],
        "StudentName": item["StudentName"],
        "LoggedInRoleCode": "FAC",
        "CourseId": ids["courseId"],
        "Sessions": [{
            "TermId": target["TermId"],
            "ModuleId": target["ModuleId"],
            "SessionId": target["SessionId"],
            "HasAttended": True,
            "AttendanceMarkedOnDate": marked_on_date_noon_karachi(),
        }],
    }

    print("\n[4] payload")
    print(json.dumps(payload, indent=2))

    if not commit:
        print("\nDRY RUN - nothing was written. Re-run with --commit to mark it.")
        return

    print("\n[5] POST save")
    r = s.post(f"{BASE}/batchmanagement/SaveStudentSessionWiseAttendanceDetails",
               json=payload, timeout=60)
    print(f"  HTTP {r.status_code}")
    print(f"  {r.text[:600]}")
    r.raise_for_status()

    print("\n[6] POST recalculate percentages")
    r2 = s.post(f"{BASE}/RecurringJob/CalculateAttendancePercentageMultipleSessions",
                json=payload, timeout=60)
    print(f"  HTTP {r2.status_code}  {r2.text[:300]}")

    print("\n[7] re-read to verify")
    after = get(s, "batchmanagement/GetCentreWiseStudentMarkAttendance",
                IsLoginRole="FAC", **ids)
    (ROOT / "tests/fixtures/markattendance_after.json").write_text(
        json.dumps(after, indent=2), encoding="utf-8")

    now = next(x for x in after["Item"]["BatchDetails"][0]["SessionDetails"]
               if x["SessionId"] == target["SessionId"])
    print(f"  IsPresent   {target['IsPresent']} -> {now['IsPresent']}")
    print(f"  Date        {target['AttendenceDate']} -> {now['AttendenceDate']}")

    wanted = date.today().isoformat()
    got = (now["AttendenceDate"] or "")[:10]
    print("\n" + "=" * 60)
    print("VERDICT")
    print("  write from Python  :", "PASS" if now["IsPresent"] else "FAIL")
    print(f"  date correct       : {'PASS' if got == wanted else 'FAIL'}  "
          f"(wanted {wanted}, got {got or 'null'})")
    print("=" * 60)


if __name__ == "__main__":
    main()

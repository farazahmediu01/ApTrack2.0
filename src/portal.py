"""
ApTrack portal adapter: HTTP and response shapes, nothing else.

No domain rules live here. This layer knows the endpoints, the header set the
gateway insists on, and the API's naming inconsistencies - and hides them
behind plain Python.

Gateway tokens live ~15 minutes. Read one from .env; never print it.
"""

import base64
import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://aptrackglobal.com/apigateway/api"
CENTRE_MAP_ID = 4985  # identifies the centre, not a student


class PortalError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# token
# --------------------------------------------------------------------------

def load_token(env: Path | None = None) -> str:
    """Read ACCESS_TOKEN from .env. Never log the value."""
    env = env or (ROOT / ".env")
    if not env.exists():
        sys.exit(f"FAIL: no .env file at {env}")
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != "ACCESS_TOKEN":
            continue
        tok = value.strip().strip("'\"")
        if not tok:
            sys.exit("FAIL: ACCESS_TOKEN is empty")
        # tolerate the value pasted with, without, or with a doubled prefix
        while tok.lower().startswith("bearer "):
            tok = tok[7:].strip()
        return f"Bearer {tok}"
    sys.exit("FAIL: ACCESS_TOKEN not found in .env")


def token_seconds_left(token: str) -> int | None:
    raw = token[7:] if token.lower().startswith("bearer ") else token
    parts = raw.split(".")
    if len(parts) != 3:
        return None
    body = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        exp = json.loads(base64.urlsafe_b64decode(body)).get("exp")
    except Exception:
        return None
    return exp - int(time.time()) if exp else None


def require_fresh(token: str, need: int = 60) -> int:
    left = token_seconds_left(token)
    if left is None:
        print("  WARN: token is not JWT-shaped; skipping expiry check")
        return 0
    if left < need:
        sys.exit(
            f"FAIL: ACCESS_TOKEN expired {abs(left) // 60} min ago.\n"
            if left <= 0 else
            f"FAIL: ACCESS_TOKEN has only {left}s left.\n"
            "  Gateway tokens live ~15 minutes. In the portal tab open DevTools\n"
            "  Console and run:  sessionStorage.token\n"
            "  Paste the value into .env as ACCESS_TOKEN= and re-run immediately."
        )
    return left


# --------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------

def marked_on(day: date) -> str:
    """
    Pin to 12:00 Karachi -> 07:00Z.

    The portal staples the current wall-clock time onto the picked date and
    converts to UTC, so a mark made at 01:00 Karachi lands on the PREVIOUS day
    (confirmed: picked 30 Jul, stored 29 Jul). At noon local the UTC and local
    calendar dates always agree.
    """
    return f"{day.isoformat()}T07:00:00.000Z"


def in_month(stamp: str | None, year: int, month: int) -> bool:
    if not stamp:
        return False
    try:
        d = datetime.fromisoformat(str(stamp)[:19])
    except ValueError:
        return False
    return d.year == year and d.month == month


MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


def month_number(name: str) -> int | None:
    return MONTHS.get((name or "").strip().lower())


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------

class Portal:
    def __init__(self, token: str | None = None, timeout: int = 60):
        self.token = token or load_token()
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({
            "authority": "aptrackglobal.com",
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9",
            "alllevel1values": "",
            "apt2_tz": "Asia/Karachi",
            "authorization": self.token,
            "content-type": "application/json",
            "origin": "https://aptrackglobal.com",
            "referer": "https://aptrackglobal.com/aptrack/",
        })

    def _get(self, path: str, **params):
        r = self.s.get(f"{BASE}/{path}", params=params, timeout=self.timeout)
        if r.status_code == 401:
            sys.exit("FAIL: 401 from the gateway - the token is stale. Paste a fresh one.")
        r.raise_for_status()
        return r.json()

    def all_students(self, cache: Path | None = None) -> list[dict]:
        """
        Every student at the centre, in one call.

        type=1 searches by enrollment id but only matches DIPLOMA students -
        short-course students (ID....... rather than Student1......) come back
        empty. type=3 ignores searchValue entirely and returns the whole centre
        (~13k rows), which does contain them.

        One request beats a per-student search, so this is the primary lookup
        path; the type=1 search stays as a fallback.
        """
        if cache and cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        rows = self._get("batchmanagement/GetCentreWiseStudentFilter",
                         centreMapId=CENTRE_MAP_ID, type=3, searchValue="a")  # value ignored for type=3, but must be non-empty
        rows = rows if isinstance(rows, list) else (rows.get("Item") or [])
        if cache:
            cache.parent.mkdir(exist_ok=True)
            cache.write_text(json.dumps(rows), encoding="utf-8")
        return rows

    @staticmethod
    def index_by_id(rows: list[dict]) -> dict[str, list[dict]]:
        idx: dict[str, list[dict]] = {}
        for r in rows:
            idx.setdefault(str(r.get("StudentId", "")).strip().lower(), []).append(r)
        return idx

    @staticmethod
    def ids_from_row(row: dict) -> dict:
        """
        NOTE the read/write naming mismatch: this endpoint calls them
        RegularBatchId and CourseMapId; every other endpoint calls them
        BatchId and StudentCourseMapId. Same values, different names.
        """
        def pick(*names):
            for n in names:
                for k, v in row.items():
                    if k.lower() == n.lower():
                        return v
            raise PortalError(f"none of {names} in filter row; keys={list(row)}")

        return {
            "studentDetailId": pick("StudentDetailId"),
            "studentCourseMapId": pick("CourseMapId", "StudentCourseMapId"),
            "courseId": pick("CourseId"),
            "batchId": pick("RegularBatchId", "BatchId"),
        }

    def find_enrollments(self, enrollment_id: str) -> list[dict]:
        """
        EVERY enrollment for one student, as id-chain dicts.

        A student can be enrolled more than once - 521 of the centre's 12,647
        are. A student on ADSE-DIRECT-FROM-TERM 5 also carries an HDSE row from
        the same batch, and an SBTE student carries a separate SBTE row. Each
        row has its own StudentCourseMapId / CourseId / BatchId and its OWN
        term list: the ADSE row holds T5+T6, the HDSE row holds T1-T4 (+SBTE).

        Taking rows[0] silently picked the HDSE row every time, which is why a
        T6 backfill found nothing and why 51 August marks landed in the wrong
        enrollment. Return them all; the caller chooses by term content.

        NOTE this endpoint returns a BARE LIST, unlike every other endpoint
        which wraps its payload in {"StatusCode":..., "Item":...}.
        """
        found = self._get("batchmanagement/GetCentreWiseStudentFilter",
                          centreMapId=CENTRE_MAP_ID, type=1, searchValue=enrollment_id)
        rows = found if isinstance(found, list) else (
            found.get("Item") or found.get("Items") or [])
        if isinstance(rows, dict):
            rows = [rows]
        want = enrollment_id.strip().lower()
        exact = [r for r in rows if str(r.get("StudentId", "")).strip().lower() == want]
        rows = exact or rows
        if not rows:
            raise PortalError(f"no student matched {enrollment_id}")
        out, seen = [], set()
        for r in rows:
            ids = self.ids_from_row(r)
            key = ids["studentCourseMapId"]
            if key in seen:
                continue
            seen.add(key)
            ids["_course"] = r.get("Course")
            ids["_batch"] = r.get("BatchName")
            out.append(ids)
        return out

    def find_student(self, enrollment_id: str) -> dict:
        """First enrollment only. Kept for the spike; new code uses
        find_enrollments() and picks by term content."""
        return self.find_enrollments(enrollment_id)[0]

    def attendance(self, ids: dict) -> dict:
        """Full per-session attendance state for ONE enrollment."""
        params = {k: v for k, v in ids.items() if not k.startswith("_")}
        return self._get("batchmanagement/GetCentreWiseStudentMarkAttendance",
                         IsLoginRole="FAC", **params)

    # ---- write path -------------------------------------------------------

    def save(self, payload: dict) -> tuple[int, str]:
        r = self.s.post(f"{BASE}/batchmanagement/SaveStudentSessionWiseAttendanceDetails",
                        json=payload, timeout=self.timeout)
        return r.status_code, r.text[:1000]

    def recalculate(self, payload: dict) -> tuple[int, str]:
        r = self.s.post(f"{BASE}/RecurringJob/CalculateAttendancePercentageMultipleSessions",
                        json=payload, timeout=self.timeout)
        return r.status_code, r.text[:400]


# --------------------------------------------------------------------------
# response shaping
# --------------------------------------------------------------------------

def unwrap(att: dict) -> tuple[dict, dict, list[dict], list[dict]]:
    """(item, batch, sessions, term_details) from an attendance response."""
    item = att["Item"]
    batch = item["BatchDetails"][0]
    return item, batch, batch["SessionDetails"], batch.get("TermDetails", [])


def build_payload(item, batch, ids, sessions, when: str) -> dict:
    """
    The save payload. Field names differ from the read side on purpose:
    HasAttended (write) vs IsPresent (read), AttendanceMarkedOnDate (write) vs
    AttendenceDate (read, misspelled in the API).
    """
    return {
        "BatchId": batch["BatchId"],
        "IsRegularBatch": True,
        "StudentDetailId": ids["studentDetailId"],
        "StudentId": item["StudentId"],
        "StudentName": item["StudentName"],
        "LoggedInRoleCode": "FAC",
        "CourseId": ids["courseId"],
        "Sessions": [{
            "TermId": s["TermId"],
            "ModuleId": s["ModuleId"],
            "SessionId": s["SessionId"],
            "HasAttended": True,
            "AttendanceMarkedOnDate": when,
        } for s in sessions],
    }

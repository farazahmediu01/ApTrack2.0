# 010 — Portal Recon: ApTrack 2.0

> **Status:** complete for Term 6 / single-student flow
> **Captured:** 2026-07-31, live session, Faculty role
> **Method:** manual walkthrough with network capture via Chrome DevTools
> **Rule for this document:** every line is something **observed**. Inferences are marked `[INFERRED]`.

---

## 1. Verdict

**This is an API job, not a browser-automation job.**

The ApTrack UI is an Angular SPA sitting on a clean REST gateway at `/apigateway/api/...`. Every operation needed for attendance marking is a plain JSON request. No DOM scraping, no Playwright, no selectors required in the execution path.

Browser automation is needed **only** for obtaining a token (see §3), and even that may be avoidable.

---

## 2. System identification

| Item | Value |
|---|---|
| Base URL | `https://aptrackglobal.com` |
| App root | `/aptrack/` (Angular SPA) |
| API gateway | `/apigateway/api/...` |
| Identity service | `/identityservice/...` |
| Realtime | `wss://aptrackglobal.com/signalR` (not needed) |
| App name | ApTrack 2.0 |
| Portal age | Launched ~July 2026 — **new, contains known bugs** (see §8) |

---

## 3. Authentication

### Observed flow

```
/aptrack/userTypeSelection?client_id=client1
        │
        ├── "Office 365"  ─────────► Microsoft SSO   (not used)
        └── "Username & Password" ─► used in this session
                    │
                    ▼
        /aptrack/tokenExchange?code=<one-time-code>&clientId=client1
                    │
                    ▼
              JWT Bearer token
```

OAuth2 authorization-code style: login yields a one-time `code`, which is exchanged for a JWT.

### Token properties

| Property | Value |
|---|---|
| Type | JWT, `HS256` |
| Issuer | `aptech.identityserver` |
| Lifetime | **30 days** (`exp - iat` = 2,592,000 s) |
| Transport | `authorization` request header |
| Claims observed | `nameid`, `unique_name`, `userId`, `userName`, `email` |

### Token refresh — observed

```
POST /identityservice/oauth2/token
     ?grant_type=refresh_token
     &code=null
     &redirect_uri=https://aptrackglobal.com/aptrack
     &client_id=client1
     &client_secret=client1secret
```

`client_id` / `client_secret` are **hardcoded public values** (`client1` / `client1secret`). The SPA refreshes its own token automatically.

`[INFERRED]` A script can therefore renew its session mid-run without re-login, given a valid refresh token.

### Post-login role gate — MANDATORY

A modal blocks the dashboard until submitted:

| Field | Observed value |
|---|---|
| Sales Organization* | `International: Aptech Worldwide` |
| Role* | `Faculty` |

This selection propagates into API calls as `IsLoginRole=FAC` / `LoggedInRoleCode: "FAC"`.
**Any automation that logs in through the UI must clear this gate.**

### Request headers observed on the save call

```
authority        aptrackglobal.com
accept           application/json, text/plain, */*
accept-language  en-US,en;q=0.9
alllevel1values  (empty)
apt2_tz          Asia/Karachi          ← see §8, date bug
authorization    <REDACTED — Bearer JWT>
```

### User context values (this account)

```
m_userid       3454
m_roleid       37
m_somasterid   27
centreMapId    4985
```

> 🔒 **Security note:** a live 30-day bearer token for this account was exposed in a chat log during recon on 2026-07-31. Password change pending. No token values are recorded in this repository.

---

## 4. Navigation path (manual, for reference)

```
Dashboard
  └─ Centre Operations
       └─ Batch Management                  /aptrack/centreOperations/batch
            └─ [Search]                     cascade: Country→Zone→Region→Area→Centre
                 └─ "Student Attendance" card → View Details
                      └─ Student Wise Attendance   /...batch/studentwiseAttendance
                           └─ Search Type = "Student ID", enter ID → [Search]
                                └─ Action column
                                     └─ Student Wise Mark Attendance
                                        /...batch/studentwiseMarkAttendance
```

**Centre context:** `ACE-PK-KARACHI-10-METRO-STAR-GATE`
`PAKISTAN → SINDH → SINDH → KARACHI` (pre-selected for this account)

**Scale observed:** 1,022 regular batches · 25,275 students · 13,683 rows in the student-attendance list.

---

## 5. The three endpoints that matter

### 5.1 Resolve Student ID → internal IDs

```http
GET /apigateway/api/batchmanagement/GetCentreWiseStudentFilter
    ?centreMapId=4985
    &type=1
    &searchValue=Student1498175
```

| Param | Meaning |
|---|---|
| `centreMapId` | numeric centre ID (4985) |
| `type` | search type; `1` = Student ID |
| `searchValue` | the student ID from the Excel |

Returns the row shown in the UI, from which the four internal IDs are derived.

**Why this hop exists:** the Excel holds `Student1498175`, but the write API needs numeric internal IDs. They are not the same thing.

### 5.2 Read full attendance state

```http
GET /apigateway/api/batchmanagement/GetCentreWiseStudentMarkAttendance
    ?studentDetailId=1450630
    &studentCourseMapId=2011204
    &courseId=9432
    &batchId=60328
    &IsLoginRole=FAC
```

**Returns every session across every term in one call** — 481 sessions for the captured student. This is the single most useful endpoint in the system.

Response shape (see fixture `tests/fixtures/markattendance_allterms_student1526980.json`):

```
StatusCode: 200
Message: "Data Retrieved Successfully!"
Item
 ├─ StudentId, StudentName, CourseName, Status, OverallCourseAttendance
 └─ BatchDetails[]
      ├─ BatchId, BatchCode, BatchName, BatchStatus, BatchAttendance, ModuleCount
      ├─ SessionDetails[]           ← 481 entries, ALL terms, marked + unmarked
      │    TermId, TermName, TermCode
      │    ModuleId, ModuleName, ModuleCode
      │    SessionId, BatchSessionMapId, RegularBatchAttendanceDetailId
      │    SessionCode, SessionName
      │    IsPresent            (bool)  ← the marked flag
      │    AttendenceDate       (ISO)   ← NOTE THE TYPO
      │    IsSkillCleared
      └─ TermDetails[]          ← 6 terms with counts
           TermId, TermCode, TermName, ModuleCount,
           TotalSessions, AttendedSessions, PendingSessions, AttendancePercentage
```

**Unmarked session looks like this:**

```json
{
  "TermId": 8296, "ModuleId": 20566, "SessionId": 191008,
  "SessionName": "RPRG-21_Session11",
  "IsPresent": false,
  "AttendenceDate": null,
  "BatchSessionMapId": null,
  "RegularBatchAttendanceDetailId": 0
}
```

A pending session is exactly `IsPresent == false`.

### 5.3 Write attendance — THE SAVE

```http
POST /apigateway/api/batchmanagement/SaveStudentSessionWiseAttendanceDetails
Content-Type: application/json
```

**Observed payload (verbatim, one session):**

```json
{
  "BatchId": 60328,
  "IsRegularBatch": true,
  "StudentDetailId": 1450630,
  "StudentId": "Student1498175",
  "StudentName": "MUHAMMAD ALI",
  "LoggedInRoleCode": "FAC",
  "CourseId": 9432,
  "Sessions": [
    {
      "TermId": 8296,
      "ModuleId": 20622,
      "SessionId": 192506,
      "HasAttended": true,
      "AttendanceMarkedOnDate": "2026-07-29T23:07:53.114Z"
    }
  ]
}
```

**`Sessions` is an array → the endpoint is bulk-capable.**
`[INFERRED]` One student's entire month can go in a single request. Not yet verified with >1 element.

### 5.4 Companion call fired after save

```http
POST /apigateway/api/RecurringJob/CalculateAttendancePercentageMultipleSessions
```

**Purpose unconfirmed.** Percentages did update after the save, but both requests fired, so it is unknown whether the save alone is sufficient. See §9.

---

## 6. Field-name gotchas

The read and write sides use **different names for the same concepts**, and the read side contains a spelling error. This is a guaranteed source of bugs.

| Concept | Read (response) | Write (payload) |
|---|---|---|
| Attended flag | `IsPresent` | `HasAttended` |
| Date | `AttendenceDate` **(sic — misspelled)** | `AttendanceMarkedOnDate` |

Do not normalise these silently. Map them explicitly and comment the typo, or someone will "fix" it later and break the parser.

---

## 7. Domain model — Term 6 (`OV-7062-T6`, TermId `8296`)

### Hierarchy

```
Student → Batch → Term (semester) → Module (book) → Session
                                                      ↑ the unit of marking
```

### Term inventory (captured student, batch 53125)

| TermId | Code | Modules | Total | Pending |
|---|---|---|---|---|
| 8291 | OV-7062-T1 | 8 | 72 | 1 |
| 8292 | OV-7062-T2 | 10 | 79 | 9 |
| 8293 | OV-7062-T3 | 7 | 80 | 11 |
| 8294 | OV-7062-T4 | 7 | 72 | 38 |
| 8295 | OV-7062-T5 | 7 | 68 | 36 |
| **8296** | **OV-7062-T6** | **9** | **110** | **83** |

### Term 6 book order — AUTHORITATIVE

User-confirmed teaching sequence, resolved to portal IDs:

| # | Book (user's name) | ModuleId | ModuleCode | Portal ModuleName |
|---|---|---|---|---|
| 1 | R Programming | `20566` | `OV-MOD-RPRG-21` | R Programming |
| 2 | Foundation of Big Data Systems | `20614` | `OV-MOD-FBGDATA-21` | Foundation of Big Data Systems |
| 3 | Processing Big Data | `20615` | `OV-MOD-PRBGDATA-21` | Processing Big Data |
| 4 | Visual Analytics with Tableau | `20622` | `OV-MOD-TABLEAU-21` | Visual Analytics with Tableau |
| 5 | Web and Social Media Analytics | `27395` | `OV-MOD-WSANALY-21` | Web and Social Media Analytics |
| 6 | eProject-Hadoop | `20610` | `OV-EP-HADOOP-21` | eProject-Processing Big Data with Hadoop |
| 7 | AI Primer | `20652` | `OV-MOD-AIPRIME-21` | AI Primer [ML, DL, Neural N/Ws ] |
| 8 | Term End Examination | `25441` | `OV-7062-T6-EXAM-21` | Term End Examination |
| 9 | Term 6-KIT | `25447` | `OV-7062KIT06` | Term 6-KIT |

Cross-check: portal reports `No of Modules: 9` for Term 6. ✅ List is complete.

> ⚠️ **Teaching order ≠ ModuleId order.** Book 6 (`20610`) has a *lower* ID than books 2–5. The sequence must come from this explicit table — never from sorting IDs.

> ⚠️ **Book names do not match portal names** (`eProject-Hadoop` vs `eProject-Processing Big Data with Hadoop`). Key configuration on `ModuleCode`, resolve to `ModuleId` at runtime. Never match on `ModuleName`.

> ⚠️ **Book order is per-term.** This table is Term 6 only. Terms 1–5 need their own tables when scope expands.

### Session ordering within a book

The UI's default row order is by `SessionId`, which does **not** match session number:

```
displayed:  Session02, Session10, Session04, Session09, Session05, Session01, ...
```

Sessions must be sorted by the **numeric suffix of `SessionName`** (`RPRG-21_Session11` → 11), not by `SessionId` and not by string sort (which puts `Session10` before `Session2`).

---

## 8. 🚨 The date bug — CONFIRMED

### What was observed

| | |
|---|---|
| Date picked in UI | **30 July 2026** |
| Sent in payload | `"2026-07-29T23:07:53.114Z"` |
| Recorded in attendance report | **29/07/2026** |

**The date was silently stored one day earlier than selected.**

### Mechanism

The SPA takes the picked date, staples on the **current wall-clock time**, then converts to UTC:

```
picked date         30 Jul 2026
+ current time      04:07:53   (Asia/Karachi, UTC+5)
= local datetime    2026-07-30 04:07:53 +05:00
→ UTC               2026-07-29 23:07:53 Z        ← previous day
```

The server stores the UTC calendar date verbatim — no conversion back to `apt2_tz`.

**Blast radius:** any marking performed between **00:00 and 04:59 Karachi time** lands on the wrong day. At month boundaries it lands in the wrong *month*. This affects existing **manual** marking, not just automation.

### Required mitigation

Always send the timestamp pinned to **12:00 local (07:00Z)**:

```
2026-07-30 12:00 +05:00   →   "2026-07-30T07:00:00.000Z"
```

At midday Karachi, the UTC and local calendar dates are identical, so the bug cannot trigger regardless of which convention the server applies.

**This is non-negotiable in the implementation.**

---

## 9. Open questions

| # | Question | Blocks | How to resolve |
|---|---|---|---|
| 1 | Does `Sessions[]` accept multiple entries in one POST? | request volume | Mark 2 sessions manually, inspect payload |
| 2 | Is `CalculateAttendancePercentageMultipleSessions` mandatory? | data correctness | Call save alone, check if % updates |
| 3 | What does a **failed** save return? | error handling | Post an invalid SessionId and observe |
| 4 | What does duplicate marking return? | idempotency | Re-mark an already-marked session |
| 5 | Can attendance be **edited** after saving? | recovery from mistakes | Look for an edit path in the UI |
| 6 | Are `BatchSessionMapId` / `RegularBatchAttendanceDetailId` ever required on write? | payload completeness | Not present in observed save; assume no |
| 7 | Can the refresh-token flow be driven without a browser? | deployment model | Attempt programmatic refresh |
| 8 | Which students are in scope? | run scope | From the Excel roster |

---

## 10. Confirmed properties that make this easy

1. **Idempotency is free.** The read endpoint reports `IsPresent` per session; the UI's pending view is just `IsPresent == false`. A re-run recomputes the delta and naturally skips already-marked sessions. Double-marking is structurally impossible if the delta is always recomputed from a fresh read.
2. **One read gives complete state.** 481 sessions across 6 terms in a single GET. No pagination needed for the read path.
3. **Stable numeric identifiers throughout.** No generated CSS classes, no fragile selectors, no scraping.
4. **The write is verified.** Marking `SessionId 192506` moved the counters:

   | Metric | Before | After |
   |---|---|---|
   | Pending | 32 | 31 |
   | Attended | 78 | 79 |
   | Term % | 70.91 | 71.82 |
   | Overall % | 100.49 | 100.73 |

---

## 11. Marking algorithm (user-specified)

**Input from Excel:** Student ID · Term/semester · a column of class dates (already MWF/TTS-correct)

```
for each student in roster:
    ids   = GetCentreWiseStudentFilter(studentId)
    state = GetCentreWiseStudentMarkAttendance(ids)

    pending = [s for s in state.SessionDetails
               if s.TermId == excel.termId and not s.IsPresent]

    dates   = excel.dates[student]          # ordered
    pairs   = []

    for book in TERM6_BOOK_ORDER:           # §7, fixed sequence
        book_sessions = sorted(
            [s for s in pending if s.ModuleId == book.moduleId],
            key=session_number                # numeric suffix of SessionName
        )
        for session in book_sessions:
            if not dates: break
            pairs.append((session, dates.pop(0)))
        if not dates: break                  # dates exhausted → stop

    POST SaveStudentSessionWiseAttendanceDetails(pairs)   # single bulk call
```

**Worked example (user-supplied):** 10 dates available. R Programming has 3 pending → those 3 consume the first 3 dates. Remaining 7 dates roll onto Foundation of Big Data Systems. Book selection is automatic; the operator never names a book or a session.

**Date handling:** every `AttendanceMarkedOnDate` pinned to `T07:00:00.000Z` per §8.

---

## 12. Artifacts from this session

| Path | Contents |
|---|---|
| `tests/fixtures/markattendance_allterms_student1526980.json` | Full read-endpoint response, 481 sessions, 6 terms. Golden fixture for parser tests. |
| `Google Attendance Management System FMO Jul-2026.xlsx` | Roster source. **Not yet analysed.** |

---

## 13. Next

1. Analyse the Excel roster → `specs/020-domain.md`
2. Resolve open questions 1–3 (cheap, one manual session)
3. Write `specs/021-behaviour.md` — failure semantics
4. Only then: `specs/030-plan.md`

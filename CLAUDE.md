# ApTrack Attendance Automation — Project Instructions

> Loaded automatically every session. The global `~/.claude/CLAUDE.md` still applies;
> this file adds project-specific rules and overrides where they conflict.

---

## 1. Purpose

Replace several hours of manual monthly work: marking attendance for 100+ students
across 5 batches on `https://aptrackglobal.com`.

**The input** is a Google Sheet management issues each month, one tab per faculty,
already filled in with `P` / `A` / `H` per student per class date.
**The output** is attendance marked in the ApTrack portal, plus a verification that
the portal's monthly count matches the sheet's.

**Recon has already proven this is an API job, not a browser-automation job.**
ApTrack 2.0 is an Angular SPA over a plain REST gateway. No Playwright, no selectors,
no headless Chrome in the execution path. If a future session starts reaching for a
browser driver, stop and re-read `specs/010-portal-recon.md`.

---

## 2. Development philosophy

**Spec before code.** `specs/` is the source of truth. Code implements a spec; it does
not invent one. If the spec is wrong, fix the spec first.

**Observed beats inferred.** Every claim in a spec is tagged as one or the other.
`[INFERRED]` means nobody has watched it happen — treat it as a risk, not a fact.
Several `[INFERRED]` lines have already turned out wrong.

**Kill binary risks first.** When one unknown could invalidate the whole architecture,
answer it with a throwaway spike before building anything on top of it. Parsing
problems are chores; "does this even work" is a risk. Risks go first.

**Dry-run is the default.** Anything that writes takes an explicit `--commit` flag.
A run with no flag must be safe to fire at any time.

**Idempotent by construction.** Re-read current state, compute the delta, write only
the delta. Never blind-write. See §5 for why this is subtler here than it looks.

---

## 3. Coding conventions

- **Python 3, standard library plus `requests`.** No dependency gets added without a
  reason that survives being questioned.
- **Secrets come from `.env`, read into memory, never printed, never logged, never
  written to a file.** No token value appears in any committed artifact.
- **Three layers, kept apart:**
  `portal adapter` (HTTP + response shapes) → `domain logic` (which sessions, which
  dates) → `orchestration` (looping, retries, reporting).
  Domain logic must be unit-testable with no network.
- Fail loudly and early with a message that says what to do next. `sys.exit("FAIL: …")`
  beats a stack trace.
- Comment the *surprises*, not the syntax. The API's inconsistencies are the thing a
  future reader will not guess.
- Fixtures captured from live responses go in `tests/fixtures/` — **gitignored**, see §6.

---

## 4. How we collaborate

Every session follows this structure. It is Faraz's, and it works:

```
1. Objective        what are we trying to accomplish today?
2. Current Knowledge what do we already know?
3. Unknowns         what information is still missing?
4. Assumptions      which assumptions should we validate?
5. Discussion       reason about the best approach
6. Decision         agree on ONE solution
7. Next Action      the single next task
8. STOP             do not continue until Faraz approves
```

**Step 8 is binding.** Do not roll past a decision point into implementation because
the next step seems obvious.

Additional standing rules for this project:

- **One milestone at a time.** No starting the next one until the current one is
  reviewed and committed.
- **Review before proceeding.** Every milestone ends with: what we learned, what
  changed, what is now known to be wrong in the specs.
- **Correct the specs when reality disagrees.** A spec that is quietly wrong is worse
  than no spec. Recon has already been wrong about token lifetime and response shapes.
- Direct feedback, `✅ Good / ⚠️ Improve / ❌ Fix`. Challenge assumptions; do not
  soften findings.
- Do not take screenshots of the portal unless asked.

---

## 5. Hard-won domain facts

Do not relearn these. Each cost real time.

**The date bug — always pin timestamps to noon Karachi.**
The portal staples the current wall-clock time onto the picked date and converts to
UTC. Marking between 00:00–04:59 Karachi lands on the *previous day*; at a month
boundary it lands in the previous *month*, which is the unit management audits.
Always send `YYYY-MM-DDT07:00:00.000Z`. Confirmed: picked 30 Jul, stored 29 Jul.

**Gateway tokens live ~15 minutes.** The 30-day JWT in the `signalR` URL is a
notification token and the gateway rejects it.

Both tokens live in **Session Storage** (not Local Storage) for
`https://aptrackglobal.com`, under the keys `token` (gateway) and `tokenSignalr`
(notifications). Quickest way to grab one: DevTools Console → `sessionStorage.token`.

**Token refresh is deliberately NOT being built for v1.** There is no refresh token in
session storage, local storage, or any readable cookie — the only httpOnly cookie is
`idp_init_client_id`, an IdP bootstrap value. The refresh URL carries `code=null`,
which points at OIDC silent renew against the identity provider's own session cookie.
Replicating that from Python means writing an OIDC client: a real sub-project.

It isn't needed. Count the requests: ~100 students × (1 read + 1 bulk write) ≈ 200
calls ≈ one minute of wall clock. That fits inside a 15-minute token many times over.
Paste one token, run, done. Revisit only if a run ever approaches the window.

**Field names differ between endpoints.** Do not "fix" these; parsing depends on them.

| Concept | Filter endpoint | Everywhere else |
|---|---|---|
| batch | `RegularBatchId` | `BatchId` |
| course map | `CourseMapId` | `StudentCourseMapId` |
| attended (read) | `IsPresent` | — |
| attended (write) | — | `HasAttended` |
| date (read) | `AttendenceDate` *(misspelled in the API)* | — |
| date (write) | — | `AttendanceMarkedOnDate` |

`GetCentreWiseStudentFilter` returns a **bare JSON list**. Every other endpoint wraps
its payload in `{"StatusCode":…, "Item":…}`.

**The portal stores presence only.** There is no "absent" record. Unmarking returns a
session to pending. So in the sheet, `A` and `H` both mean simply *do not mark* —
count the `P`s and nothing else.

**Idempotency is NOT free here.** Session identity can't be double-marked, but the
algorithm's input is a *count*, so a second run would mark N more sessions. The guard:

```
already  = sessions in <term> whose AttendanceMarkedOnDate falls in <month>
to_mark  = count_of_P_in_row - already
```

This is also the resume mechanism, and it measures exactly what management audits.

**Ordering.** Sort sessions by the numeric suffix of `SessionName`, never by
`SessionId` and never as a string. Book order is a configured sequence keyed on
`ModuleCode` — teaching order does not match ModuleId order.

**Never cross a term boundary.** If a student has fewer pending sessions in the term
than their `P` count, mark nothing for that student and report it. Shuffling books
and sessions within a term is fine; shuffling terms is forbidden.

**Hard cap: 14 sessions per student per month.** Never mark more, under any
circumstance. If a student's `P` count exceeds 14, mark nothing for that student and
report it — the teacher handles the exception manually. This is both a business rule
and the blast-radius guard: it bounds what any bug can do to one student's record.

**Known anomaly, unresolved.** One test mark is stuck in production: `SessionId
184616` (`EP-HADOOP-21_Session01`), dated Sunday 2 Aug 2026 — not a class day. The
portal's Modify Attendance screen cannot currently locate it; filtering by session id
returns nothing. The idempotency guard in §5 absorbs it (the student gets one fewer
session marked in August, so the monthly *count* still reconciles), but the
session-to-date pairing for that student is wrong. Revisit when the script can unmark
via the API — worth testing whether `HasAttended: false` on the save endpoint works as
a programmatic undo, which would also give us a rollback path.

---

## 6. Student data — non-negotiable

**This repository has a public-facing GitHub remote.** Real student names, enrollment
IDs, and attendance histories must never be committed.

- `tests/fixtures/*.json`, `*.xlsx`, and `images/` are gitignored. Keep it that way.
- If a fixture is needed for tests, **anonymise it** and commit the anonymised copy
  under `tests/fixtures/anon/`.
- No student identifier is hardcoded in any script — pass it as an argument.
- Before any commit, check the diff for enrollment IDs (`Student1NNNNNN`) and names.

---

## 7. Layout

```
specs/      numbered specifications — the source of truth
spikes/     throwaway experiments that answer one question each
tests/      fixtures (gitignored) and, in time, actual tests
ROADMAP.md  the process document: idea → deployed
```

# Attendance Domain Rules

> Self-contained reference for anyone — human or sub-agent — working on ApTrack
> attendance automation. It carries the rules, not the code. Load this instead of
> re-deriving the domain from the portal.
>
> Every claim is tagged `[OBSERVED]` (someone watched it happen) or `[INFERRED]`
> (nobody has). Treat `[INFERRED]` as a risk. Several have already turned out wrong.
>
> Companion documents: `CLAUDE.md` §5 (portal/API facts), `specs/010-portal-recon.md`
> (endpoint recon, contains four known-wrong claims).

---

## 1. The job in one paragraph

Management issues a Google Sheet each month, one tab per faculty, with `P`/`O`/`A`/`L`/`H`
codes per student per class date. We read it and mark the matching number of sessions
in the ApTrack portal, then verify the portal's monthly count matches the sheet's.
**What management audits is a count per student per term-month** — not which specific
session was marked.

---

## 2. Status codes

`[OBSERVED]` — all five codes plus blank appear in the July 2026 roster.

| Code | Meaning | Counts as attendance? |
|---|---|---|
| `P` | Present | **Yes — mark it** |
| `O` | Online | **Yes — mark it** |
| `A` | Absent | No |
| `L` | Leave | No |
| `H` | Holiday | No |
| *(blank)* | not enrolled yet / not recorded | No |

**Only two outcomes exist.** The portal stores *presence only* — there is no "absent"
record, and unmarking returns a session to pending. So `A`, `L`, `H` and blank all
collapse to the same instruction: **do not mark**. Do not build separate handling for
them. `[OBSERVED]`

Compare codes case-insensitively and strip whitespace.

**The output of parsing a student's row is a count**, per term (§4). Which dates the
codes fell on does not survive into the marking step (§5 explains why).

Blanks are meaningful as a signal even though they are not marked: a run of leading
blanks means a late joiner. One such student appears in the July roster.

---

## 3. Roster sheet structure

`[OBSERVED]` — `data/Attendence-sheet/Google Attendance Management System FMO Jul-2026.xlsx`,
faculty Mr. Syed Ashir Ali, one sheet, 5 batch blocks, 127 students.

One faculty tab holds **several batch blocks stacked vertically**, each in the same
format. Blocks are *not* evenly spaced — block height follows the student count.
**Find blocks by marker, never by row arithmetic.**

Per block, relative to the row containing `Attendance Sheet` in column A:

| Offset | Content |
|---|---|
| `+0` | `Attendance Sheet` · `Batch Start Date` (J) · the date (M) |
| `+1` | `Batch Code` (A/B) · `Faculty` (C/D) · `Days` (F/G) · `Time` (I/J) · `Month` (L/N) · `Year` (T/U, **first block only**) |
| `+2` | header row: `S. No` `Enrollment #` `Student Name` `Course`, then **date columns** |
| `+3…` | student rows |
| *(varies)* | footer: `Module`, `Session Covered as per CPC`, `Actual Session Covered`, `Total Flow`, `Faculty Name` — labels in **column D**, values from column E rightwards |

### Traps

**The `S. No` column runs past the last student.** It is pre-numbered to a fixed
capacity and students fill from the top. Terminate the student scan on a blank
**`Enrollment #` *and* `Student Name`**, never on blank column A. `[OBSERVED]`

**Date columns vary per block: 13 or 14**, driven by the `Days` pattern (`TTS` =
Tue/Thu/Sat, `MWF` = Mon/Wed/Fri). Detect them by testing the header cell for a real
date type, not by a fixed column range. `[OBSERVED]`

**An empty-string cell sits at column R** in the 13-date blocks. It is not a date
column. A type check excludes it; a truthiness check does not. `[OBSERVED]`

**Merged ranges exist** (40 in the July file). Only the top-left cell of a merge
carries a value; the rest read as `None`. Read labels by searching, not by fixed
coordinates. `[OBSERVED]`

**The sheet name can contain control characters** — the July tab is
`Mr. Syed Ashir Ali\t` (a literal tab, `_x0009_` in the XML). Do not match sheet
names exactly. `[OBSERVED]`

**Hundreds of blank rows follow the last block** (the July sheet reports 998 rows for
197 rows of content). The last block's nominal range runs to the end of the sheet;
the student-row terminator is what actually bounds it. `[OBSERVED]`

**`Total Flow` and `Faculty Name` are not usable.** `Faculty Name` is empty in every
block of the July file. `Total Flow` now holds a single `S-N` value in the last date
column (`S-7`, `S-13`, `S-2`, `S-4`) whose meaning is undocumented and which does
**not** equal `Session Covered as per CPC` minus `Actual Session Covered`. Neither
row is used by anything. `[OBSERVED]`

---

## 4. Curriculum, term and module resolution

**Term boundaries MAY be crossed.** *(Rule reversed 2026-08-31 after faculty checked
with management: they count the number of sessions marked, and do not care which term
carried them.)* An earlier version of this document forbade it. It was wrong.

**Fallback order — current term, then earlier terms ascending, then later terms
ascending.** From T5 the walk is `5 → 1 → 2 → 3 → 4 → 6`. Earlier terms come first
deliberately: they are the backlog that would otherwise never be filled.

If every term is exhausted, mark what is available and report *"N marked, no pending
sessions remain across all terms"* — that student's three-year diploma is 100% marked
and **they should be skipped in all future months.** `[OBSERVED as a rule; the
100%-complete case has not yet been seen in live data]`

### Step 1 — course, from the Batch Code prefix

| Prefix | Programme | Course code | CPC file |
|---|---|---|---|
| `PR2-` | ACCP-PRIME-2 | 7062 | `data/CPC/CPC-7062.xlsx` |
| `AI-` | ACCP-AI | 7144 | `data/CPC/CPC-7144.xlsx` |

`[OBSERVED]`. An unknown prefix must **stop the batch**, not fall back to a default.

### Step 2 — starting term, from the **Module row**

**Use the `Module` row. Do NOT depend on either session-index row.**

`Session Covered as per CPC` is the plan the batch has fallen behind — it differs from
reality by up to 32. `Actual Session Covered` is closer to the truth *when it is
populated correctly*, and in the July (Ashir) sheet it was: all five batches
cross-checked cleanly against the Module row.

**But it is frequently corrupt.** In the August (Faraz) sheet, three of six batches
have a broken Actual row: `[OBSERVED]`

| Batch | Defect |
|---|---|
| `PR2-202310F+…` | contains a literal `0`; skips 440 |
| `PR2-202310G+…` | runs **backwards** at the last column (433 → 432) |
| `PR2-202311G+…` | **constant at 388** for all 13 columns; never increments |

The `Module` row has been intact in every block of every sheet seen. Resolve the
starting term from it, and use `Actual Session Covered` only as a cross-check that
warns when the two disagree. Flag a corrupt Actual row in the run report.

Because the fallback order (below) crosses terms anyway, the starting term only
decides *which* sessions get consumed, never *how many*. A wrong answer here is
recoverable; a wrong count is not.

### Step 3 — term, from the CPC `Semester` column

The CPC workbook has two sheets:

- **`CPC`** — the `Batch Planed CPC` table. Column C = class (cumulative session)
  number, column E = `Semester`, written **once at each boundary and blank
  thereafter** (forward-fill it). **This column is the authority for term
  boundaries.**
- **`courses`** — column D = cumulative session index, column E = session code
  (`MDB 03`, `VAT12`). Strip the trailing number for the book abbreviation.

The `courses` sheet also carries certification markers (`CPISM`, `DISM`, `HDSE I`…)
in a scattered column. **They give different boundaries and are wrong.** Use the
`CPC` sheet's `Semester` column. `[OBSERVED]`

**Term boundaries differ per course — this is why the course must be resolved first:**

| Term | Semester | 7062 | 7144 |
|---|---|---|---|
| 1 | CPISM | 1–70 | 1–70 |
| 2 | DISM | 71–142 | 71–148 |
| 3 | HDSE I | 143–225 | 149–225 |
| 4 | HDSE II | 226–295 | 226–304 |
| 5 | ADSE I | 296–361 | 305–385 |
| 6 | ADSE II | 362–469 | 386–469 |

`[OBSERVED]`, derived from both CPC files. The 7062 column matches the table faculty
supplied from memory, exactly. A table previously derived from a portal fixture
(72/79/80/72/68/110) was **wrong and has been withdrawn.**

### Step 4 — module abbreviations are course-scoped

The roster abbreviates books its own way, and **the same abbreviation means different
things in different curricula**:

| Sheet writes | Under 7062 | Under 7144 |
|---|---|---|
| `R` | `RPRO` — R Programming | `REACT` — Frontend Web Development with ReactJS |
| `MDB` | `MDB` — Large Data Management with MongoDB | `MONGO` — Managing Large DataSets with MongoDB |
| `E-Pro` | `PROJECT` | `ePro` |

`[OBSERVED]`. **Never resolve a module name without a course.** This also disposes of
the old "eProject is ambiguous" problem: eProject is never resolved by name at all —
the session index resolves it, and the name is only ever a cross-check.

July 2026 books seen: `R`, `FBDS`, `MDB`, `E-Pro`, `MUI`, `Azure`, `React`.

### Programme structure — what a "term" actually is

`[OBSERVED — faculty, 2026-08-31]`

| Programme | Length | Semesters |
|---|---|---|
| **HDSE** (Higher Diploma) | 2 years | S1 frontend · S2 PHP/Laravel · S3 .NET · S4 Flutter |
| **ADSE** (Advanced Diploma) | 3 years | the same four, then **S5 MERN stack** · **S6 R + Big Data** |
| **SBTE** (Sindh Board of Technical Education) | — | English, Oracle, Maths, Physics |

SBTE students enter after the four HDSE semesters to earn the ADSE+SBTE certificate.
Some upgrade to ADSE as well, so SBTE runs **in parallel** with S5 and S6.

**This script's scope is ADSE/HDSE semesters 1–6 only. SBTE is never marked.**
Its CPC does not exist yet and a separate script will handle it, along with digital
marketing and the other manual work.

### Identifying SBTE in the portal

Term structures seen across 91 real students: `[OBSERVED]`

| TermCode | Students | Sessions per term |
|---|---|---|
| `OV-7062-T1…T6` | 62 | 72 / 79 / 80 / 72 / 68 / 110 — ADSE, **all markable** |
| `OV-7066-T1…T5` | 10 | 72 / 79 / 80 / 72 / **104** |
| `OV-1334-OFF-T1` | 2 | 21 — short course |

7066's T1–T4 are **identical** to 7062. Its T5 is a single 104-session block — that is
the SBTE content, not a normal semester. **Exclude `OV-7066-T5`**, plus any term whose
code or name contains `SBTE`.

Excluding it moved two students into the "short" report, correctly: one had pending
sessions only inside SBTE, and one is **100% complete across all six ADSE semesters** —
that student should be skipped in every future month.

Term completeness varies a lot (14 students start at T3, three at T5, two at T2). The
portal is new and batch data is inconsistent, which is exactly why attendance is
handled **student-wise rather than batch-wise.** `[OBSERVED]`

### Course 7066

7066 students follow **the same semesters, books and sessions as 7062 through term 4**.
Terms 5 and 6 are **SBTE** (Sindh Board of Technical Education) and have a different
CPC, not yet supplied. So `CPC-7062` is safe for a 7066 student up to session 295;
beyond that it is not. `[OBSERVED — faculty]`

Note this is why the **Batch Code prefix**, not the per-student `Course` column,
determines the batch's curriculum: within one block the `Course` column holds a mix
(`ADSE`, `HDSE`, `-DT2`, `-DT3`, `-DT5` variants across 7062, 7144 and 7066).

### Short courses

Batch codes with no diploma prefix (`MS-202608D`, course `OV-1334-OFFICE16`) are
**short courses**. They have no semesters and no CPC — sessions are a flat numbered
list. Mark them term-agnostically, in portal order.

**A short course starting mid-month leaves leading columns blank, not `A`.** The
August batch started 11 Aug, so the four columns for 1–8 Aug are empty for every
student. Blank is already "do not mark", so this needs no special handling — but do
not mistake it for missing data. `[OBSERVED]`

Enrollment IDs in short courses use a different format (`ID…`) from diploma students
(`Student1……`). Do not assume one shape. `[OBSERVED]`

---

## 5. How many sessions to mark

### Sequential book fallback

Fill **strictly in chronological teaching order**, earliest book first, until that
book's sessions are exhausted, then fall back to the next book in sequence — staying
inside the term.

Order sessions by the **numeric suffix of `SessionName`** (`HADOOP-21_Session07` → 7).
Never sort by `SessionId`, and never sort the name as a string.

### Book order comes from the CPC, matched by module name

The authoritative teaching order is the CPC's own session index. The portal's
`ModuleName` and the CPC's `Subjects` column spell books identically
(`Foundation of Big Data Systems`, `R Programming`), so the two join on name with no
mapping table. `[OBSERVED]`

Match on the **longest** matching name, not the first. Plain substring matching puts
`Processing Big Data` (398–415) inside `eProject-Processing Big Data with Hadoop`
(450–459) — a different book, two books later.

### The portal's own ordering is not teaching order

`GetCentreWiseStudentMarkAttendance` returns sessions grouped by module (modules *are*
contiguous in the array), but: `[OBSERVED]`

- module order within a term is roughly alphabetical, **not** teaching order;
- **36 of the modules** in a single student's record have their `SessionName` numbers
  out of ascending order inside the array.

So neither the array order nor `ModuleId` can be trusted. **Derive teaching order from
the student's own history instead:** for each module take the earliest `AttendenceDate`
among its already-marked sessions and sort modules by it. Modules never taught have no
date and sort last, by code, deterministically. This is self-calibrating and needs no
configuration file.

### Absence drift is correct behaviour, not a bug

Session-to-date pairing intentionally drifts. A student absent in week 1 still gets
their next session marked in week 2 — the backlog rolls forward. This is right,
because management counts *how many* sessions were marked in the term-month, not
*which*. Do not "fix" it.

### Idempotency is NOT free

Session identity cannot be double-marked, but the algorithm's input is a **count** —
so a naive second run marks N *more* sessions. Always compute the delta:

```
already = sessions in <term> whose AttendanceMarkedOnDate falls in <month>
to_mark = count_of_valid_codes_in_term - already
```

`to_mark <= 0` means nothing to do. This doubles as the resume mechanism and measures
exactly the number management audits. `[OBSERVED]`

### Hard cap: 14 sessions per student per month

**Never mark more than 14, under any circumstance.** If a student's valid-code count
exceeds 14, **mark nothing for that student and report them** — the teacher handles
the exception manually.

This is both a business rule and the blast-radius guard: it bounds what any bug can do
to one student's record. Apply it to the *count from the sheet*, before the delta.

A 14-date block plus a perfect attendance row lands exactly on the cap, so the cap
is live in normal data, not just in error cases: nine students hit exactly 14 in July.
`[OBSERVED]`

### The holiday / boundary interaction

`Actual Session Covered` **increments on holiday columns too** — a known sheet defect
faculty has confirmed. The index therefore reads one high after each holiday. Inside a
wide book this is harmless. **On a term boundary it is not**: it can put a column on
the wrong side of the line. Flag any batch where a holiday precedes a boundary and
verify against the portal before committing. `[OBSERVED]`

---

## 6. Guard rails

1. **Dry-run is the default.** Writes require an explicit `--commit`.
2. **Never blind-write.** Re-read current state, compute the delta, write the delta.
3. **Pin every timestamp to noon Karachi — `YYYY-MM-DDT07:00:00.000Z`.** The portal
   staples wall-clock time onto the date and converts to UTC; a mark made at 01:00
   Karachi lands on the previous day, and at a month boundary in the previous *month*,
   which is the unit management audits. Confirmed: picked 30 Jul, stored 29 Jul.
   `[OBSERVED]`
4. **Report, never guess.** Held students (over cap, unresolvable curriculum,
   insufficient pending sessions) must appear in the run report by ID and reason.
5. **No student identifier is hardcoded in any script** — the repository is public.
   Pass identifiers as arguments; mask names and enrollment IDs in output by default.

---

## 7. Locked sessions — the save endpoint lies

Some pending sessions in earlier terms **cannot be marked** because that term's
transcript has already been generated. The portal greys their checkboxes.

**A locked session returns HTTP 200 from the save endpoint and then does not
persist.** There is no error, no non-200 status, no message. `[OBSERVED — first
production run, 2026-08-31: three sessions across three students returned 200 and
were still `IsPresent: false` on re-read.]`

Consequences, all mandatory:

- **Never trust a save response.** The only proof a mark landed is a re-read.
- **Verify after every write**, then compare `IsPresent` per `SessionId`.
- **Replace, do not abort.** Blacklist the session that failed and pick the next
  candidate, or the student silently ends up short of the count management audits.

No pre-flight flag has been found. `IsSkillCleared` is **not** it — it is `true` on
176 of 178 pending sessions in a full-record fixture, independent of `IsPresent`.
`TermDetails.PendingSessions` gives the right *upper bound* on availability but does
not separate markable from locked.

**Locking is common, not rare.** The August 2026 run hit **76 locked sessions** across
91 students, and they cluster hard in the early terms: `[OBSERVED]`

| Module | Refused | Term |
|---|---|---|
| `EP-PHP` | 15 | T2 |
| `FMENTRE` | 13 | T2/T3 |
| `JSCRIPT` · `MYSQL` · `PHPWEB` | 8 each | T2 |
| `LARA` | 7 | T2 |
| `BSTRAPJQ` · `ASPCORE` · `DISGIT` · `SEOWEB` | 3–4 each | T1–T3 |
| `AZURE` · `RPRG` · `EP-DOTNETASP` | 1–2 each | T4–T6 |

Consistent with the transcript explanation: the further back the term, the more likely
its transcript is closed. Sessions in the batch's **current** term have never been
refused.

Budget for it: give the replacement loop enough rounds that one run finishes
(`MAX_REPLACEMENT_ROUNDS = 10`). A student deep in the backlog can burn several rounds
before landing a markable session.

### Exams and kits are not classes

Every term carries a `Term End Examination` (`OV-…-EXAM-…`) and a `Term N-KIT`
(`OV-…KIT…`). They appear in `SessionDetails` and are pending, but they are not
classes and must never be marked as attendance. `[OBSERVED]`

The principled test is the CPC: **if a curriculum is loaded and it does not contain
the module, do not mark it.** Exams and kits are absent from the CPC; every real book
is present. Match on the portal's `ModuleName`, which spells books exactly as the
CPC's `Subjects` column does.

---

## 8. Enrollment status — report it, never act on it

`Item.Status` is usually `Enrolled`. Twelve students across the six August batches
read **`FDO`** (Financial Drop Out). `[OBSERVED]`

**Do not skip them.** Skipping was implemented and reverted the same day: most of
those students still attend class and it is the *portal record* that is stale, not
their attendance. Silencing twelve real students is far worse than marking a genuinely
dropped one. Show the status as a tag beside the row; leave the decision to the
teacher.

## 9. The report's `Employee Name` column is not the writer

Management's downloadable Student Attendance Report carries an `Employee Name` column.
It is **a function of `SessionName`, not of who recorded the attendance** — across 67
distinct sessions, not one carries two different employees. `[OBSERVED]`

It shows the faculty **assigned to that session in the batch's session map**. So rows
written by this script through one faculty's token still come back under five
different names, including other teachers and a generic centre account
(`Mr. General Exam  MSG`). The save payload contains no employee field at all, so the
script cannot influence it.

Do not treat a mismatch here as a bug. Related: the portal holds duplicate employee
records for one person (`Faraz  Ahmed` and `Mr. Faraz  Ahmed` both appear).

### Unexplained: a student missing from the report

One student's six August marks are present in `GetCentreWiseStudentMarkAttendance`
and absent from the downloaded report. `FDO` is **not** the explanation — another FDO
student in the same batch appears normally. Course, batch id and term all look
ordinary. `[OBSERVED — unresolved]` Re-check when the next monthly report is pulled.

## 10. Open questions

- **What identifies a locked session before writing?** (§7)
- **The SBTE CPC for 7066 terms 5–6.**
- What does `Total Flow`'s `S-N` value mean? Currently unused.
- Does the portal's own term/session numbering match the CPC's cumulative index? The
  cross-check so far is sheet-internal (Actual row vs Module row); it has **not** been
  checked against a live portal read for these batches.
- Does the save endpoint's `Sessions[]` accept multiple entries in one call?
- Is the recalculate-percentages call redundant? The save response already says
  *"and percentages updated"*.
- Does `HasAttended: false` work as a programmatic unmark? Would give a rollback path
  and would resolve the stuck mark on `SessionId 184616`.
- What does a *failed* save return? Only success responses have been observed.

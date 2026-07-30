# APTRACK Attendance Automation — Architecture & Workflow Roadmap

> **Status:** pre-implementation. No code yet, by design.
> **Project:** automate monthly attendance marking for hundreds of students on `https://aptrackglobal.com/`.
> **Author's context:** experienced Python developer, new to Claude Code as an AI-assisted development environment.
> **Date:** 2026-07-30

---

## Why this project is worth doing properly

It's small, real, has a hard deadline every month, and its failure modes are *visible* (wrong attendance = angry students). That makes it a natural forcing function for the two weakest skills in the inventory — **specs** and **tests**.

Before the roadmap, one architectural point that matters more than everything else below.

---

## 0. The single decision that shapes this project

Beginners frame this as "an AI agent that browses the portal and clicks things." That's the wrong runtime. Ask instead:

> **Where does each part of this system sit on the determinism spectrum?**

```
DISCOVERY (once, exploratory)          EXECUTION (monthly, hundreds of records)
─────────────────────────────          ────────────────────────────────────────
Non-deterministic is fine              Non-deterministic is unacceptable
LLM + agentic browser                  Plain deterministic code
Cost/latency irrelevant                Must be fast, idempotent, auditable
Claude in Chrome, you watching         Playwright / httpx, no LLM in the loop
```

**Use the agent to reverse-engineer the portal once. Use generated deterministic code to run it forever.**

An LLM re-deciding which checkbox to click, 400 times, once a month, is expensive, slow, and — worse — *silently* wrong in ways you won't notice until a student complains.

And push that logic one step further. During discovery your real goal isn't "find the selectors." It's:

> **Find the write path.** What HTTP request actually persists an attendance record?

Portals like this almost always do one of three things:

1. One POST per student row (AJAX)
2. One bulk POST per class/section with an array of student IDs
3. A full form submit per page

If it's the bulk POST, your "browser automation" collapses into a session cookie plus a few dozen HTTP calls, and you've eliminated the entire fragile layer. That discovery is worth hours of your life every month. Most people never look, because they went straight to clicking.

**So: browser automation is the fallback, not the default.** Decide that with evidence in Phase 1 — not by assumption.

### Authorization note

This is an employer system. You have an account and you already do this task by hand, so automating your own work is normal. But it's a **bulk write to a system of record shared with other people**. Get a one-line sign-off from whoever owns the portal at Aptech, and never put anyone's credentials but your own in the project. That's not bureaucracy — it's what makes this a portfolio piece you can *talk about* in an interview instead of one you have to hide.

---

## 1. The phase map: idea → deployed

Each phase has exactly one artifact. **If the artifact doesn't exist, the phase isn't done.**

| # | Phase | Artifact | Approx. effort |
|---|---|---|---|
| 0 | **Frame the outcome** | `specs/000-outcome.md` — what "done" means in business terms | 30 min |
| 1 | **Recon** | `specs/010-portal-recon.md` — real observed facts about the portal | 1–2 hrs, hands-on |
| 2 | **Spec** | `specs/020-*.md` — behaviour contracts (no tech choices) | 1–2 hrs |
| 3 | **Plan** | `specs/030-plan.md` — architecture, tech decisions, data model | 1 hr |
| 4 | **Tasks** | `specs/040-tasks.md` — ordered, individually-verifiable slices | 30 min |
| 5 | **Build** | working code, one task at a time | the bulk |
| 6 | **Prove** | test suite + a golden fixture set | continuous |
| 7 | **Harden** | dry-run mode, retries, run manifest, kill switch | 1 session |
| 8 | **Deploy** | scheduled trigger + notification | 1 session |
| 9 | **Operate** | monthly run log, drift detection | forever |

Note phase 1 sits **before** the spec. This is the part people get wrong: you cannot write a useful spec for a system you haven't observed. Every hour spent speculating about the portal's DOM is an hour of fiction. **Go look first.**

---

## 2. How to decompose this into specs

Don't write "the spec." Write a small set of documents, each answering a different question, each independently reviewable.

### `000-outcome.md` — the outcome contract

Written as if for a person you're hiring. What does this worker achieve? For whom? What's explicitly out of scope? What must it *never* do?

Non-goals worth stating aloud:

- Never modifies grades
- Never enrolls or removes students
- Never marks a student absent when data is missing — fails loudly instead

Include a success metric: *"a month's attendance for N students completed in under X minutes with zero incorrect records, verified by spot-check."*

### `010-portal-recon.md` — observed reality

- Login flow and auth mechanism (session cookie? JWT? SSO redirect? MFA?)
- Navigation path to attendance
- Page structure and pagination
- **The exact network request that writes a record, with its payload shape**
- What the response looks like on success and on failure
- What happens on double-submit
- Whether records can be edited after saving
- Every stable identifier you can find (student IDs, section IDs, `data-*` attributes)

**Every line here must be something you saw, not something you assume.** Mark inferences explicitly.

### `020-domain.md` — the data model and rules

- What is a "student", a "session", an "attendance record"?
- What statuses exist (present / absent / leave / holiday)?
- Where does the input come from — a roster, an Excel file, a default-all-present rule with an exceptions list?
- What's the **idempotency key** that says "this student, this date, already marked"?

This spec is pure domain logic and has nothing to do with browsers.

### `021-behaviour.md` — behaviour and failure semantics

Written as scenarios:

> *Given* the roster has 412 students and 3 are already marked, *when* the run executes, *then* 409 are written, 3 are skipped as already-marked, and the manifest reports both counts.

Then the ugly ones — for each, decide **retry, skip-and-continue, or abort**, now, on paper:

- Session expires mid-run
- Portal returns 500 on student 200 of 412
- A student in your roster doesn't exist in the portal
- The DOM changed and a selector no longer matches

This document is your test suite in prose; you'll translate it almost line-for-line into tests.

### `022-security.md`

Where credentials live, how the session is stored between runs, what must never appear in a log or screenshot, who can trigger a run.

### `030-plan.md`

**Only now** do you name Playwright vs httpx, sync vs async, SQLite vs JSONL for state, Task Scheduler vs GitHub Actions. Every choice gets one sentence of justification tied back to a spec line. If a tech choice doesn't serve a spec requirement, cut it.

### `040-tasks.md`

Slice into units you can verify independently:

1. Config + secrets loading
2. Login and session persistence
3. Roster parsing
4. Read current attendance state for a section
5. Mark a **single** student
6. The loop with idempotency
7. Run manifest + logging
8. Dry-run mode
9. Notification
10. Scheduling

Notice #5 comes before #6 — get one student right, end to end, before you touch hundreds. Steps 4 and 5 are your first real checkpoint: if `read state` works, you can write tests for everything downstream **without mutating anything**.

---

## 3. Driving Claude Code through it

### Project `CLAUDE.md` first

Before anything else, create one in this directory. It should state:

- This is a spec-driven project; specs live in `specs/`
- No code may be written that isn't traceable to a task in `040-tasks.md`
- The runtime must be deterministic — **no LLM calls in the execution path**
- Never commit credentials

This file is how you make the discipline structural instead of willpower-based — it loads into every session in this folder, including future ones where you've forgotten your own rules.

### Use plan mode for design work, not chat

Press `Shift+Tab` twice (or ask Claude to enter plan mode). In plan mode Claude can read and explore but not write — so you get architecture discussion without 400 lines of Playwright you didn't ask for. Approve the plan, *then* let implementation start. For a spec-driven project this maps perfectly: **plan mode for phases 0–4, normal mode for phase 5+.**

### Spec-Kit for the skeleton

`/specify` → `/plan` → `/tasks` gives you scaffolding and, more importantly, breaks the start-of-project freeze. The project is small enough that the ceremony can't bury you.

### One task per session, one commit per task

Point Claude at a single task from `040-tasks.md`, have it implement that plus its test, verify it runs, commit. Resist "implement the whole thing." Long unreviewed generations in browser automation are where fragility gets baked in — the AI will happily invent a selector strategy you'd have rejected on sight.

### Git from commit zero

You need the diff review loop. `git diff` is your primary defence against AI-generated drift.

### Subagents for research, not for building

The `Explore` agent is right for "search the docs for how Playwright persists auth state." Don't hand a subagent the build — you lose the review loop that's the whole point.

### Hooks, once you're rolling

A `PostToolUse` hook running your linter/tests after every edit turns "did I break it" into an automatic answer. Worth setting up around task 3–4, not day one.

### The review format matters

When Claude generates code, ask for it reviewed as ✅ / ⚠️ / ❌ — and specifically ask it to flag **brittle selectors** and **missing failure handling**, because those are the two things AI-generated browser code is systematically worst at.

---

## 4. Tooling, and why

### Claude in Chrome (MCP browser tools) — discovery phase only

The highest-leverage tool available right now. It drives *your* Chrome, with your existing login, while you watch.

The killer feature isn't clicking — it's **`read_network_requests`**. You navigate to the attendance page, mark one student by hand, and Claude reads the network log to tell you exactly what request fired, with what payload, to what endpoint. That's the write path, found in five minutes.

Also: `read_page` for structure and stable selectors, `read_console_messages` for errors.

**Not a production runtime:** needs a live browser, an extension, and an LLM in the loop.

### Playwright (Python) — the production runtime, if you need a browser

- Auto-waiting (kills most flakiness people blame on "slow sites")
- Role- and text-based locators that survive CSS churn
- `storage_state` for saving a logged-in session to disk and reusing it
- Headed *and* headless from the same code
- Trace/video capture on failure

`playwright codegen` records your manual clicks into a first draft — a great input to hand Claude for cleanup, much better than letting it guess.

### httpx + the discovered API — the best case

If recon finds a clean write endpoint, this is your runtime. Orders of magnitude faster, dramatically more reliable, trivially testable, no browser to install on the scheduling machine.

You'd still use Playwright for login if auth is complex, then hand the cookies to httpx. **This hybrid is the professional answer and the likely destination.**

### Deliberately NOT in the runtime: an LLM

Not "an agent that reads the page and decides." You have a deterministic task with a known schema and hundreds of repetitions. Reserve intelligence for genuinely ambiguous parts — and in this project, that's zero parts, once recon is done.

*(Selenium: also fine, but Playwright's auto-waiting and locator model make it strictly less painful. No reason to choose it new.)*

---

## 5. Designing scheduled execution

Think of the schedule as a *trigger* wrapped around a **pure, idempotent run function**. Get the ordering right:

```
   [ trigger ]          [ guard ]           [ execute ]          [ report ]
  cron / manual   →   already run this  →  read state,        →  manifest,
  / CLI flag          month? lock file      write deltas          log, notify
                      to prevent
                      concurrent runs
```

Design principles, in order of importance:

1. **Idempotent by construction.** Running twice must not double-mark. Achieved by *reading current state first* and computing a delta — never by blind writes. This one property removes most of the fear from scheduled automation.
2. **On-demand and scheduled share one code path.** The scheduler calls the same entry point you call by hand, with the same flags. Never a separate "scheduled version."
3. **`--dry-run` is the default.** Real writes require an explicit flag. Make the dangerous thing require typing.
4. **Every run emits a manifest** — timestamped: what it attempted, wrote, skipped, failed, how long it took, and a hash of the roster input. Your audit trail when someone disputes a record, and your drift detector when the portal changes.
5. **Guard rails on blast radius.** If the computed delta exceeds a sane threshold (e.g. more records than students on the roster), abort and notify. A bug that marks 4,000 records is a much worse day than a bug that marks none.
6. **Resumable.** Persist progress per student so a crash at 300/412 resumes rather than restarts.

### Where to run it

- **Windows Task Scheduler** — the honest starting point. The machine you're on, minimal moving parts, zero new concepts. **Do this for v1.**
- **GitHub Actions on a cron** — the better answer *once* secrets are solved and the portal is reachable from the internet. The version worth putting on your resume: it forces containerization, secret management, and log retention. **Migrate later as a deliberate exercise, not on day one.**

### Notification

At minimum: success/failure with counts, to somewhere you actually read. Silent automation is how you discover in March that February never ran.

---

## 6. Reliability and maintainability

### Selectors are the whole ballgame

Prefer, in order:

1. An API call (no selector at all)
2. A stable `id` / `data-*` attribute
3. An accessible role plus text
4. Structural CSS

**Never** a generated class name or an absolute XPath. Centralize every selector in one module so a UI change means editing one file, and comment each one with *when* you verified it against the live portal.

### Fail loudly, never guess

If a student isn't found, or a status is ambiguous, or the page looks unfamiliar — stop and report. The temptation is to make automation "robust" by ploughing through anomalies. For a system of record, **an aborted run is cheap and a wrong record is expensive.** Encode that asymmetry.

### Separate the three layers

| Layer | Knows about | Changes when |
|---|---|---|
| **Portal adapter** | HTTP / DOM | the portal's UI changes |
| **Domain logic** | attendance rules | your business rules change |
| **Orchestration** | runs, retries, reporting | rarely |

Only the adapter should change when the portal changes — and that's the only layer that's hard to test. If your attendance rules are tangled into your click code, every UI tweak risks your business logic.

### Testing — three tiers

- **Unit** — domain logic against fixtures. Pure functions, no network, milliseconds. Roster parsing, delta computation, idempotency keys.
- **Contract** — replay saved real HTTP responses and DOM snapshots captured during recon. **This is your golden dataset:** save actual HTML/JSON from the portal into `tests/fixtures/`. When the portal changes, these tests fail and *tell you what changed* — drift detection for free.
- **Smoke** — one real run against the live portal in dry-run mode, marking nothing. Run before every real execution.

> The fixture-capture habit is the highest-value practice in this whole document. It's how you get tests for something as untestable-seeming as a third-party web portal — and it's the same technique used for evals later in the roadmap.

### Observability

Structured logs (JSONL, one line per student). Never log credentials or session tokens. Screenshot + Playwright trace on any failure. Keep the last N run manifests.

### Maintainability

Pin dependency versions (a Playwright minor bump changing behaviour mid-run is a real thing). Write a `RUNBOOK.md` — how to run it, how to interpret a failure, what to do when the portal changes. You'll need it in six months, and it's the artifact that turns a script into a product.

---

## 7. Where beginners go wrong

1. **Skipping recon.** Writing the spec from imagination, then discovering the portal works nothing like assumed. Look first.
2. **Never checking for an API.** Building an elaborate clicking machine on top of an endpoint that would have taken one POST. Check the network tab. Always.
3. **Putting the LLM in the runtime.** Slow, costly, non-reproducible, unauditable — for a task with zero ambiguity once recon is done.
4. **Testing at full scale immediately.** Running the loop over 400 students on attempt one. One student. Dry run. Then ten. Then all.
5. **No dry-run mode.** Building the write path first and the safety switch never. Build them together.
6. **No idempotency.** Then a mid-run crash, a re-run, and duplicate records nobody can untangle.
7. **Accepting a large AI code drop unreviewed.** The model will write plausible-looking Playwright with brittle selectors and no error handling, and it'll pass your one happy-path test. Small slices, reviewed diffs.
8. **Brittle selectors** copied from Chrome DevTools' "Copy selector." Those break on the next CSS build.
9. **Credentials in the repo.** Or in a log. Or in a screenshot of the login page. `.env` + `.gitignore` from commit one.
10. **Optimizing for speed over correctness.** Hammering the portal with concurrent requests. Add deliberate pacing — you're a guest on someone else's server, and one run per month has no performance requirement worth risking a rate-limit ban over.
11. **Silent failure.** No notification, no manifest. Discovering months later that it stopped working.
12. **Treating "it worked once" as done.** The portal *will* change. The question is whether you find out from your fixture tests or from a student.

---

## 8. Where this sits in the roadmap

This is a **Digital FTE**, not a script — *"the Attendance Clerk."* 168 hours a week of availability, replacing several hours of manual labour monthly, in an encoded domain (education admin). Every Aptech-like institute on earth has this exact problem, which makes it a genuine Path B portfolio piece and not a toy.

Pillars it hits directly:

| Pillar | Gap closed |
|---|---|
| **#7 SDD** | worst habit gap — this project is small enough to actually finish the spec |
| **#6 TDD / Evals** | #1 critical gap — fixture-based contract tests are the on-ramp |
| **Browser automation** | stated domain focus |
| **#9 Cloud deployment** | when the schedule migrates to GitHub Actions |

Also a clean **Outcome Architect** exercise — `000-outcome.md` *is* an outcome spec.

It does not conflict with Step 1 of the plan. Ch62 L4 handoffs stay the main line; this is the SDD-and-tests debt paid down in parallel, on a project with a real deadline.

---

## 9. Next two actions

### 1. Run a recon session in the browser, this week

You log into the portal, Claude is attached to Chrome. You mark **one** student by hand. Claude reads the network log and page structure, and you find out whether this is an API job or a Playwright job. That single answer determines the entire architecture, and everything from phase 2 onward depends on it. Nothing gets written to the portal that you don't click yourself.

### 2. Before that, in the next 20 minutes

- `git init` here
- Write `CLAUDE.md`
- Write `specs/000-outcome.md`

Two files, no code. That's the artifact for today — and the first spec file written in ten sessions.

---

*Recommended order: recon first, then let its findings inform the outcome spec.*

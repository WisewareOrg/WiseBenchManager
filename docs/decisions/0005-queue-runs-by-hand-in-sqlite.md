# 0005 — Queue runs by hand in an SQLite table beside the records; each tick takes one before the merge queue's head

**Status:** accepted
**Decided:** 2026-11-12 (WisewareOrg/WiseBenchManager #60 Queue behaviour)
**Rev:** 1

## Revisions

- **rev 1** — 2026-11-12 — first written (#60 Queue behaviour).

## Context

A run on the one physical board is also wanted for a ref that is not the merge queue's head: an
investigation of a branch, a re-run after a bench fix. Before this decision such a run was a
foreground `bench test` that refused whenever the board was busy, so it had to be retried by hand
until the board was free, and the merge queue's runs and the hand runs had no common order.

What the choice is held to:

- Runs on the board stay one at a time, started by the timer of ADR 0003 rev 2, with the lock and
  the `DISABLED` marker unchanged.
- A maintained tool before code of our own, for the store and the exclusion both.
- A run that is taken is never taken twice.

## Decision

**A systemd timer still starts one `bench run` per tick, and each tick takes one entry: the oldest
run queued by hand, else the merge queue's head.**

1. **The wait stays one tick.** It is about a minute against runs of ten minutes to four hours, and a
   resident process would need the queue on disk anyway.
2. **Hand entries come first, oldest first.** A merge-queue entry waiting on GitHub's timeout behind
   a hand run is accepted.
3. **The hand queue is one SQLite table in `records/queue.db`, through Python's `sqlite3`.** Ids
   come from AUTOINCREMENT, so an id `bench test` printed never names another run. Removing an entry
   and taking one are each a single `DELETE … WHERE id = ?`, which exactly one caller wins. The
   queue libraries built on it either took the same entry twice across processes (persist-queue,
   queuelib, measured), lack a list and a delete-by-id (litequeue), or add a quiet dependency whose
   defaults evict and pickle for no fewer lines (diskcache). The job runners (pueue, task-spooler,
   nq) are second schedulers.
4. **`Queue` is the one entry point.** `add`, `entries` and `remove` serve the terminal and the
   watch view; `take` serves `bench run`. The order is defined once, in `entries()`.
5. **The merge queue is read live, as its refs show it, the head only.** `head()` still runs
   `git fetch origin`, so `entries()` fetches. No snapshot of the merge queue is kept, so none can
   go stale during a run.
6. **The commit is resolved when the run is queued, in the bench checkout, without a fetch.** The run
   builds that commit and refuses a dirty tree when it starts, and its record reads
   `kind=test ref=<as typed>`. A run queued by hand may carry `--skip-build`; a merge-queue run
   never skips the build.
7. **A run queued by hand posts nothing and builds no check client.** A failing hook or test in it
   is recorded and is not the bench's fault; any other failure, including a checkout that fails, is
   the bench's and writes `DISABLED`.
8. **An entry is deleted when taken.** A running entry cannot be removed, and a run that ends in the
   bench's failure is not queued again.
9. **While `DISABLED` is present, entries wait and queueing still works.** The way to run one now is
   `bench run` by hand.
10. **Queueing needs neither `LG_ENV` nor the board lock,** and is done from the bench checkout.

## Alternatives considered

- **A resident manager process in place of the timer.** It would start a queued run at once, but it
  needs the queue on disk anyway, an IPC channel, a different meaning for the `SIGTERM` that ends an
  overlong run, and a restart that kills the run in progress to load new code. Rejected: the saving is
  about a minute.
- **A queue library or a job runner.** See decision 3: each was measured or read and rejected there.
- **A hand-written spool directory** (one file per entry). Rejected: it is our code doing what SQLite
  does, with its own id scheme, ordering and atomic claim.
- **Reading the merge queue in full through GitHub's GraphQL API.** Rejected: the refs show only the
  entry GitHub is building, and the head is all the bench runs.

## Consequences

- **The hand queue is state between runs,** kept in `records/queue.db`.
- **`bench test` shows no PASS or FAIL;** the verdict is in the record, read with `bench status --watch`.
- **A waiting hand entry runs only when the merge-queue read succeeds:** a `git fetch` or `ls-remote`
  fault, or more than one qualifying `gh-readonly-queue` ref, writes `DISABLED` as it does on any tick,
  before the hand entry runs.
- **A hand entry is deleted when taken:** if its run ends in a bench fault, the record shows `run
  error`, `DISABLED` is written, and the entry is not re-queued.
- **An unreadable `queue.db` is a bench fault** (`sqlite3.DatabaseError`); recovery removes the file,
  and the runs queued by hand in it are lost.
- **An entry queued outside the bench checkout, or with another `BENCH_RESULT_DIR`, is never taken.**
- **`entries()` fetches,** so a view must not call it at a refresh rate.

**Premise that would reopen this:** hand runs needing to start within seconds; GitHub building more
than one merge-queue entry at a time; organisation policy permitting a resident bench process.

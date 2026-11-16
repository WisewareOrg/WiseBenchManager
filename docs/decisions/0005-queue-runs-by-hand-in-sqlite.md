# 0005 — Queue runs by hand in an SQLite table beside the records; each tick takes one before the merge queue's head

**Status:** accepted
**Decided:** 2026-11-16 (WisewareOrg/WiseBenchManager #72 Queue tab shows the whole merge queue)
**Rev:** 2

## Revisions

- **rev 2** — 2026-11-16 — the queue lives in the clone `bench` is installed from, and the ref
  resolves in that clone's `checkout/`, or in a directory named with `-C`, from any directory
  (decisions 3, 6 and 10; the "never taken" consequence removed; three alternatives added; #74
  one-bench defect).
- **rev 1** — 2026-11-16 — first written (#60 Queue behaviour; #23 Queue view; #72 Queue tab shows
  the whole merge queue).

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
run queued by hand, else the first merge-queue entry GitHub has built.**

1. **The wait stays one tick.** It is about a minute against runs of ten minutes to four hours, and a
   resident process would need the queue on disk anyway.
2. **Hand entries come first, oldest first.** A merge-queue entry waiting on GitHub's timeout behind
   a hand run is accepted.
3. **The hand queue is one SQLite table in the clone's `records/queue.db`, through Python's `sqlite3`.** Ids
   come from AUTOINCREMENT, so an id `bench test` printed never names another run. Removing an entry
   and taking one are each a single `DELETE … WHERE id = ?`, which exactly one caller wins. The
   queue libraries built on it either took the same entry twice across processes (persist-queue,
   queuelib, measured), lack a list and a delete-by-id (litequeue), or add a quiet dependency whose
   defaults evict and pickle for no fewer lines (diskcache). The job runners (pueue, task-spooler,
   nq) are second schedulers.
4. **`Queue` is the one entry point.** `add`, `entries` and `remove` serve the terminal and the
   watch view, `preview` the watch view alone; `take` serves `bench run`. The order is defined once, in `entries()`.
5. **The merge queue is read live and in full, from GitHub's GraphQL API.** `Queue` reads
   `repository.mergeQueue(branch: "main").entries` through PyGithub, as the bench's GitHub App of
   `bench.toml`'s `[github]` table, and lists every entry in GitHub's order with its PR number, title
   and merge-group commit, which GitHub gives an entry only once it builds it. `take()` reads
   the queue, then runs `git fetch origin`, which brings the commit of every entry still queued or
   merged when it runs, and takes the first entry that has one; `entries()` fetches nothing. No
   snapshot of the merge queue is kept, so none can go stale during a run.
6. **The commit is resolved when the run is queued, in the bench's own checkout,
   `<clone>/checkout`, without a fetch of `origin`.** The run builds that commit and refuses a dirty
   tree when it starts, and its record reads `kind=test ref=<as typed>`. A run queued by hand may
   carry `--skip-build`; a merge-queue run never skips the build. With `-C <dir>`, the ref resolves
   in `<dir>` instead and the commit is fetched from `<dir>` into the checkout by
   `git fetch --no-write-fetch-head <dir> <sha>`, which writes objects only.
7. **A run queued by hand posts nothing and builds no check client.** A failing hook or test in it
   is recorded and is not the bench's fault; any other failure, including a checkout that fails, is
   the bench's and writes `DISABLED`.
8. **An entry is deleted when taken.** A running entry cannot be removed, and a run that ends in the
   bench's failure is not queued again.
9. **While `DISABLED` is present, entries wait and queueing still works.** The way to run one now is
   `bench run` by hand.
10. **Queueing needs neither `LG_ENV` nor the board lock,** and is done from any directory: `bench` acts on the clone it is installed from.

## Alternatives considered

- **A resident manager process in place of the timer.** It would start a queued run at once, but it
  needs the queue on disk anyway, an IPC channel, a different meaning for the `SIGTERM` that ends an
  overlong run, and a restart that kills the run in progress to load new code. Rejected: the saving is
  about a minute.
- **A queue library or a job runner.** See decision 3: each was measured or read and rejected there.
- **A hand-written spool directory** (one file per entry). Rejected: it is our code doing what SQLite
  does, with its own id scheme, ordering and atomic claim.
- **Reading the merge queue from the `gh-readonly-queue` refs with `git ls-remote`.** Rejected: GitHub
  makes a ref only for the entry it is building, so the Queue tab showed that entry alone.
- **Keeping the fetch in `entries()`**, with `--no-optional-locks`. Rejected: a fetch from the watch
  can race the tick's own fetch on ref locks and fail the run, which writes `DISABLED`.
- **Skipping the watch's reads while a run is live.** Rejected: the Queue tab would stay stale for the
  whole run.
- **State relative to the working directory.** Rejected: every directory `bench` was started in
  became its own bench, with its own queue, history and lock, so a second run could drive the board
  beside the timer's and a watch showed nothing of a run started elsewhere.
- **Pinning each fetched commit with a ref per entry.** Rejected: a ref write brings back the race
  on ref locks with the tick's `git fetch origin`.
- **Fetching from `<dir>` when the entry is taken.** Rejected: the source tree would have to still
  hold the commit when the entry runs.
- **Accepting the collision risk.** Rejected: a watch could then disable the bench.

## Consequences

- **The hand queue is state between runs,** kept in the clone's `records/queue.db`.
- **`bench test` shows no PASS or FAIL;** the verdict is in the record, read with `bench status --watch`.
- **A waiting hand entry runs only when the merge-queue read succeeds:** a `git fetch` fault or a
  failed read of GitHub's API writes `DISABLED` as it does on any tick, before the hand entry runs.
- **A hand entry is deleted when taken:** if its run ends in a bench fault, the record shows `run
  error`, `DISABLED` is written, and the entry is not re-queued.
- **An unreadable `queue.db` is a bench fault** (`sqlite3.DatabaseError`); recovery removes the file,
  and the runs queued by hand in it are lost.
- **A commit fetched from `-C <dir>` has no ref pinning it.** Git's default two-week prune grace is
  what keeps it for a queued wait.
- **`take()` fetches and `entries()` does not,** so a view's read writes nothing into `.git` and cannot
  collide with a run's git. `entries()` still reaches GitHub's API, bounded by PyGithub's own timeout,
  so a view reads it off its draw path.

**Premise that would reopen this:** hand runs needing to start within seconds; GitHub building more
than one merge-queue entry at a time; organisation policy permitting a resident bench process.

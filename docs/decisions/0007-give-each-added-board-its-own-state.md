# 0007 — Give each added board its own state, its switch included

**Status:** proposed
**Decided:** not decided; proposed 2026-11-25 (S14 multi-board spike), two points open below
**Rev:** 1

## Revisions

- **rev 1** — 2026-11-25 — first written (S14 multi-board spike).

## Context

The bench drives one board, so a second board beside it idles while a run is on the first. One bench
per host was the rule because the runner was never designed for more than one board, and what
conflicts on the host is several builds at once. Several boards per host is a requirement. It
replaces the line of #74 one-bench defect that leaves a second board out. What the choice is held to:

- Only one build runs on the host at a time, whichever board it is for.
- Each board's queue is its own.
- A board other than the merge queue's is turned on and off by its own command, which neither reads
  nor changes anything of the merge queue.
- A one-board install keeps its directories, commands, records and configuration as they are.

## Decision

**An added board is a `[boards.<name>]` table in `~/.config/wisebench/bench.toml` and its own
directory in the clone.**

1. **The first board has no name and no table.** Every command without `--board` is the first board.
2. **An added board is `boards/<name>/checkout` and `boards/<name>/records`.** Its queue, lock,
   `DISABLED` and records are in that directory, and its own fault stops the timer of the first
   board's unit with `@<name>`. The table gives `lg_env`, its labgrid file; a run on it sees one
   `LG_` variable, `LG_ENV`, so that file cannot take the first board's address.
3. **A board has its own checkout.** A run's suite runs from its tree to the end, a `--skip-build`
   run included, so with one tree a second board's run changes the tests under the first.
4. **The runner holds no lock across boards.** A build is the layer's procedure, so the layer's one
   build entry point takes the host's build lock and waits for it. The wait is inside the run's own
   stage, so it counts in `TimeoutStartSec`.
5. **Only the first board reads the merge queue.** An added board's queue lists runs queued by hand,
   builds no `Check` and posts nothing.
6. **The switch of an added board is the file `ON` in its records directory, absent at first.**
   `bench on <name>` makes it, `bench off <name>` removes it, and nothing else changes. Off, `bench
   run --board <name>` exits 0 before it touches anything; queueing still works, and a run in
   progress finishes. It is not `DISABLED`: clearing a fault never turns a board on.

## Open

#74 also lists these as not wanted, and this work touches them:

1. **View changes:** the watch shows an added board's name, and `ON` or `OFF`.
2. **Requirements on the layer:** its build entry point takes the host's build lock.

## Alternatives considered

- **One checkout and one run at a time, with the queues kept apart.** Rejected: the second board
  idles as it does without this.
- **A build lock in the runner, held for a built run.** Rejected: a layer's suites start builds too,
  and so do people by hand, so a lock here covers less than every build and keeps runs apart that
  build nothing.
- **A git worktree per board.** Rejected: worktrees share refs, so two ticks' `git fetch` can collide
  on ref locks, the race ADR 0005 rev 1 names.
- **One queue table with a board column.** Rejected: it needs a migration, and one wrong query drives
  the other board.
- **The timer as the switch.** Rejected: a `bench run` by hand ignores it.

## Consequences

- **Two boards' runs overlap freely,** so an added board needs a layer whose build entry point takes
  the host's build lock, and whose hooks and suites share no fixed port or name on the host.
- **An added board costs a clone of the layer and a build directory.**
- **Two labgrid files that name one board drive it under two locks,** and nothing says so.
- **The code does not check who runs `bench on` or `bench off`.**

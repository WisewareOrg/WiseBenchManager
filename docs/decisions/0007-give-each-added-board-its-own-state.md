# 0007 — Give each added board its own state, its switch included

**Status:** proposed
**Decided:** not decided; proposed 2026-11-25 (#92 more than one bench board)
**Rev:** 1

## Revisions

- **rev 1** — 2026-11-25 — first written (#92 more than one bench board).

## Context

The bench drives one board, so a second board beside it idles while a run is on the first. One bench
per host was the rule because the runner was never designed for more than one board, and what
conflicts on the host is several builds at once. Several boards per host is a requirement. It
replaces the line of #74 one-bench defect that leaves a second board out. What the choice is held to:

- Each board's queue is its own.
- A board other than the merge queue's is off until it is turned on, by its own command, which
  neither reads nor changes anything of the merge queue.
- A one-board install keeps its directories, commands, records and configuration as they are.

## Decision

**An added board is a directory of the clone, `boards/<name>/`, with its own checkout, labgrid file
and state.**

1. **The first board has no name and no such directory.** Every command without `--board` is the
   first board.
2. **An added board is `checkout/`, `env.yaml` and `records/` in its directory.** Its queue, lock and
   records are there. A name is looked up among the directories that hold a `checkout`, so one that
   is not there, `..` among them, ends the command with exit 2 before anything is made.
3. **A board has its own checkout.** A run's suite runs from its tree to the end, a `--skip-build`
   run included, so with one tree a second board's run changes the tests under the first.
4. **A run on an added board sees one `LG_` variable, `LG_ENV`, naming its `env.yaml`.** labgrid
   substitutes every `LG_` variable in that file, so another one could hold the first board's
   address.
5. **The runner holds no lock across boards.** Two boards' runs overlap. A layer whose builds must
   not overlap keeps them apart itself; a wait there is inside the run's own stage, so it counts in
   `TimeoutStartSec`.
6. **Only the first board reads the merge queue.** An added board's queue lists runs queued by hand,
   builds no `Check` and posts nothing.
7. **An added board runs while `ON` is in its records directory and `DISABLED` is not; `ON` is
   absent at first.** `bench on <name>` makes it, `bench off <name>` removes it, and neither touches
   the queue or a timer. Without it `bench run --board <name>` exits 0 before the queue, git or any
   hook. Queueing still works, and a run in progress finishes.
8. **A failure of the bench on an added board leaves it off, with its reason, and stops no timer.** No
   command leaves a board on and disabled: `bench on` removes `DISABLED` and says what it said, and
   `bench off` leaves it.

## Alternatives considered

- **One checkout and one run at a time, with the queues kept apart.** Rejected: the second board
  idles as it does without this.
- **A build lock in the runner, held for a built run.** Rejected: a layer's suites start builds too,
  and so do people by hand, so a lock here covers less than every build and keeps runs apart that
  build nothing.
- **labgrid's coordinator and places.** They arbitrate a board among several users and hosts.
  Rejected: here one host drives each board, so there is nothing to arbitrate, and a queue, a checkout
  and a switch per board would still be ours, beside a service to keep running.
- **A git worktree per board.** Rejected: worktrees share refs, so two ticks' `git fetch` can collide
  on ref locks, the race ADR 0005 rev 1 names.
- **One queue table with a board column.** Rejected: it needs a migration, and one wrong query drives
  the other board.
- **A table per board in the bench's settings file.** Rejected: its one field was the path of the
  file that already defines the board.
- **`DISABLED` as the switch.** Rejected: absent would mean on, so a missed step in setting a board
  up, or a lost records directory, would turn it on.
- **`bench on` leaving `DISABLED`.** Rejected: it would say the board is on and nothing would run.
- **The timer as the switch.** Rejected: a `bench run` by hand ignores it.

## Consequences

- **An added board costs a clone of the layer and a build directory.**
- **The watch shows one board at a time;** its Boards tab lists them all and chooses one.
- **Two labgrid files that name one board drive it under two locks,** and nothing says so.
- **The code does not check who runs `bench on` or `bench off`.**

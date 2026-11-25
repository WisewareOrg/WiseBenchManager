# 0007 — Give each added board its own state, its switch included

**Status:** proposed
**Decided:** not decided; proposed 2026-11-25 (S14 multi-board spike), three points open below
**Rev:** 1

## Revisions

- **rev 1** — 2026-11-25 — first written (S14 multi-board spike).

## Context

The bench drives one board, so a second board beside it idles while a run is on the first. What the
choice is held to:

- Only one build runs on the host at a time, whichever board it is for.
- Each board's queue is its own.
- A board other than the merge queue's is turned on and off by its own command, which neither reads
  nor changes anything of the merge queue.
- A one-board install keeps its directories, commands, records and configuration as they are.
- The runner knows no board by name; a board is a name in the bench's own settings file.

## Decision

**An added board is a `[boards.<name>]` table in `~/.config/wisebench/bench.toml` and its own
directory in the clone.**

1. **The first board has no name and no table.** It is `checkout/`, `records/`, `LG_ENV` from
   `bench.env`, `[run] unit` and `[github]`. Every command without `--board` is the first board.
2. **An added board is `boards/<name>/checkout` and `boards/<name>/records`.** Its table gives
   `lg_env`, the labgrid file that names the board. `queue.db`, `lock`, `DISABLED` and the records
   hang off that one directory, so nothing in them is shared, and its own fault stops the timer of
   the first board's unit with `@<name>`. A name is letters, digits, `-` and `_`; any other, or one
   with no table, ends the command with exit 2. `bench run` refuses, with exit 2, when two boards'
   labgrid files, the first board's `LG_ENV` included, are one file. An added board's run sees one
   `LG_` variable, `LG_ENV`, so its labgrid file cannot take the first board's address.
3. **A board has its own checkout** (open point 1). A run checks out its commit and its suite runs
   from that tree to the end, a `--skip-build` run included, so with one tree a second board's run
   changes the tests under the first.
4. **The runner holds no lock across boards** (open point 3). What may not overlap on the host is
   the layer's: its hooks and suites start the builds, so the layer's one build entry point takes the
   host's build lock and waits for it. A wait is inside the run's own stage, so it counts in
   `TimeoutStartSec`.
5. **Only the first board reads the merge queue.** An added board's queue lists runs queued by
   hand, builds no `Check` and posts nothing.
6. **The switch of an added board is the file `ON` in its records directory, absent at first.**
   `bench on <name>` makes it and `bench off <name>` removes it; both leave the queue, `DISABLED`
   and the timer alone. `bench run --board <name>` without it exits 0 before the lock, the queue,
   the checkout or the board is touched. Queueing works while a board is off, and a run in progress
   finishes when its board is turned off. It stays as set across a reboot (open point 2). The words
   are not the view's `:enable` and `:disable`, which act on the timer and `DISABLED`.

## Open

Built one way, each a small change to the other:

1. **A second layer checkout with its own build directory** per added board. The other way: one
   checkout, one run at a time across boards.
2. **The switch stays as set across a reboot.** The other way: off at every boot.
3. **The build lock is the layer's,** in its one build entry point. The other way: in the runner,
   held for a built run.

## Alternatives considered

- **One checkout and one run at a time, with the queues kept apart.** Rejected: the second board
  idles as it does without this.
- **A build lock in the runner, held for a built run.** Rejected: a build is the layer's procedure,
  started by its suites too and by hand outside the runner, so a lock here covers less than every
  build and keeps runs apart that build nothing.
- **A git worktree per board.** Rejected: worktrees share refs, so two ticks' `git fetch` can
  collide on ref locks, the race ADR 0005 rev 1 names.
- **One queue table with a board column.** Rejected: it needs a migration, and one wrong query
  drives the other board.
- **A `unit` key in the table.** Rejected: one that names another board's unit stops that board's
  timer, and the unit is derivable.
- **The timer as the switch.** Rejected: a `bench run` by hand ignores it, and a reboot re-arms it.
- **A second clone of this repository.** Rejected: one host has one bench.
- **One labgrid file with several targets.** Rejected: labgrid's `target` fixture requires a target
  named `main`, so the layer's fixtures would have to know of boards.

## Consequences

- **Two boards' runs overlap freely,** so an added board needs a layer whose build entry point takes
  the host's build lock, and whose hooks and suites share no fixed port or name on the host.
- **An added board costs a clone of the layer and a build directory.**
- **Two labgrid files that name one address are not seen.** Only one file named twice is refused.
- **`bench.env` is shared,** so its `PYTEST_ADDOPTS` reaches an added board's suites too.
- **The switch is not a permission.** Whoever can run `bench` can flip it.

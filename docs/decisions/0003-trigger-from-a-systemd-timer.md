# 0003 — Start `bench run` from a systemd user timer under `flock`, not a CI runner

**Status:** accepted
**Decided:** 2026-10-11 (the runner survey that founded this repository; built in WisewareOrg/WiseBenchManager#6 `bench run`)
**Rev:** 1

## Revisions

- **rev 1** — 2026-10-29 — first written (WisewareOrg/WiseBenchManager PR #31 ADR 0001 as trade studies).

## Context

The merge queue's head is gated by a run on the one physical board, from the build host beside it.
Something has to notice that the queue has an entry, start a run for it, keep a second run off the
board while one is going — including a hand `bench test` started for an investigation — and get the
verdict back to GitHub.

GitHub has a mechanism built for exactly this, and it is the obvious answer: a self-hosted Actions
runner listening for `merge_group`. It would select the entry, serialise jobs and report status
with no code of ours. Pulling the other way, it is a long-lived registered service on the build
host. What the choice is held to:

- Organisation policy forbids a long-lived registered CI runner on the build host.
- A maintained tool before code of our own, for the trigger and the mutual exclusion both.
- A hand run and a queued run never share the board.

## Decision

**A systemd user timer starts `bench run`; an `fcntl.flock` keeps the board to one run.**

- Each tick reads the merge queue's head from the `gh-readonly-queue` refs, taking the entry whose
  base is `main`'s current tip. With none, it exits.
- `bench run` and `bench test` share one lock file beside the records. A tick that finds it held
  backs off; a hand run that finds it held refuses.
- Reading the queue head is one class, `Queue.head()`, so a different queue is a different class.

## Alternatives considered

- **A self-hosted GitHub Actions runner on `merge_group`.** GitHub's own mechanism: entry selection,
  job serialisation and status reporting all for free, and no queue-reading code of ours. Rejected:
  it is a long-lived registered runner on the build host, which organisation policy forbids, and the
  timer and the lock already give the trigger and the exclusion with tools the host runs anyway.

## Consequences

- **A queue entry waits for the next tick** before a run picks it up, and for the current job too if
  the lock is held.
- **Queue-head selection is our code**, a `git ls-remote` read kept behind one class, where an
  Actions runner would have done it for free.
- **The bench stops itself.** The timer is the unit the bench's own failure disables, so a broken
  bench does not keep failing on every tick; restarting it is a hand step.

**Premise that would reopen this:** the organisation's CI policy changing what may run on the build
host, which makes the Actions runner the pre-analysed alternative.

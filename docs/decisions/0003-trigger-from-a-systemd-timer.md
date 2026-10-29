# 0003 — Start `bench run` from a systemd user timer under `flock`, not a CI runner

**Status:** accepted
**Decided:** 2026-10-11 (WisewareOrg/WiseBenchManager#6 `bench run`: the lock, the queue head, the job)
**Rev:** 1

## Revisions

- **rev 1** — 2026-10-29 — first written.

## Context

The merge queue's head is gated by a run on the one physical board, from the build host beside it. A
hand `bench test` uses the same board and must never run alongside a queued job. What the choice is
held to:

- Organisation policy forbids a long-lived registered CI runner on the build host.
- A maintained tool before code of our own, for the trigger and the mutual exclusion both.

## Decision

A systemd user timer starts `bench run`. It reads the merge queue's head from the
`gh-readonly-queue` refs with `git ls-remote`, and holds an `fcntl.flock` on a lock file it shares
with `bench test`: a timer tick that finds the lock held backs off, and a hand run that finds it held
refuses.

## Alternatives considered

- **A self-hosted GitHub Actions runner on `merge_group`.** Rejected: it is a long-lived registered
  runner on the build host, which organisation policy forbids; the timer and the lock already give
  the trigger and the exclusion.

## Consequences

- Merges serialise at the length of one build-and-test job on one board; this is accepted.
- A queue entry waits up to one timer interval before a run picks it up.
- The timer is the unit the bench's own failure disables, so a broken bench stops itself.
- Reopened if the organisation's CI policy changes what may run on the build host.

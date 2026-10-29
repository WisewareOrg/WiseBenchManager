# 0003 — Start `bench run` from a systemd user timer under `flock`, not a CI runner

**Status:** accepted
**Decided:** 2026-10-11 (the runner survey that founded this repository; built in WisewareOrg/WiseBenchManager#6 `bench run`)
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

A systemd user timer starts `bench run`, which takes the merge queue's head. It holds an
`fcntl.flock` on a lock file it shares with `bench test`: a timer tick that finds the lock held
backs off, and a hand run that finds it held refuses.

## Alternatives considered

- **A self-hosted GitHub Actions runner on `merge_group`.** Rejected: it is a long-lived registered
  runner on the build host, which organisation policy forbids; the timer and the lock already give
  the trigger and the exclusion.

## Consequences

- Merges serialise at the length of one build-and-test job on one board; this is accepted.
- Reopened if the organisation's CI policy changes what may run on the build host.

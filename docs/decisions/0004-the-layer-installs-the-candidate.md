# 0004 — Install the candidate from the layer's own fixture; the runner owns no device procedure

**Status:** accepted
**Decided:** 2026-10-13 (WisewareOrg/WiseBenchManager#18 remove the runner's install stage)
**Rev:** 1

## Revisions

- **rev 1** — 2026-10-29 — first written.

## Context

A built run's target suite must run against the commit that run just built, so something has to put
that build on the board. labgrid has no RAUC strategy, so the install code is written by someone
either way; the question is who owns it. What the choice is held to:

- The runner knows nothing of the application or the board beyond what the layer tells it.
- A device procedure has one owner, the one whose path is exercised on the real board.

## Decision

The runner names the built commit to the target suite through the `bench_candidate` fixture,
`None` outside a built run, and touches the board no further. The layer's own session fixture reads
it and installs that commit through the layer's own update procedure, before any target-suite test
runs.

## Alternatives considered

- **The runner installs the bundle itself** (labgrid `put`, `rauc install`, reboot). Rejected: it
  duplicated the layer's own update procedure and the two drifted. Two of about eight of the
  runner's installs on the bench board were cut off mid-write when the board's hardware watchdog
  reset it; no such failure is known on the layer's own path, which has run hundreds of times. The
  runner's install was tested only against a fake driver, where that class of failure cannot show.

## Consequences

- A layer with a different update mechanism needs only its own fixture reading `bench_candidate`,
  not a runner change.
- The runner's config and record carry the commit only, never a bundle path or hash.
- That the booted board runs the candidate is checked by the layer's first target-suite test, not
  guaranteed by the runner.

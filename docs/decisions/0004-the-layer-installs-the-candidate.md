# 0004 — Install the candidate from the layer's own fixture; the runner owns no device procedure

**Status:** accepted
**Decided:** 2026-10-13 (WisewareOrg/WiseBenchManager#18 remove the runner's install stage)
**Rev:** 1

## Revisions

- **rev 1** — 2026-10-29 — first written (WisewareOrg/WiseBenchManager PR #31 ADR 0001 as trade studies).

## Context

A built run's target suite has to run against the commit that run just built, so something puts that
build on the board first. labgrid has no RAUC strategy, so the install code is written by someone
either way. The question is **who owns the procedure**: the runner, which every layer would share,
or the layer, which already has an update path of its own that it uses by hand.

The generic answer has a real case: RAUC is not specific to any one layer, an install strategy is
labgrid's documented extension point, and a runner-owned install could be upstreamed. Pulling the
other way, a device procedure that exists twice drifts, and only one of the two is exercised every
day. What the choice is held to:

- The runner knows nothing of the application or the board beyond what the layer tells it.
- A device procedure has one owner: the one whose path runs on the real board.
- The build a run tests is the one that run built, and the record can show it.

## Decision

**The runner names the build; the layer installs it.**

- The runner's pytest plugin gives the target suite a `bench_candidate` fixture: the commit this run
  built, `None` outside a built run.
- The layer's own session fixture reads it and installs that commit through the layer's own update
  procedure, before any target-suite check runs.
- The runner touches the board no further. The layer's first target-suite check asserts the booted
  board names that commit.

## Alternatives considered

- **The runner installs the bundle itself** (labgrid `put`, `rauc install`, reboot, slot check), as
  an `Ota` class or a labgrid `RaucStrategy`. One install path every layer shares, upstreamable to
  labgrid, and the runner could guarantee rather than check which build is on the board. Rejected
  on the evidence: it duplicated the layer's own update procedure and the two drifted. Two of about
  eight of the runner's installs on the bench board were cut off mid-write when the board's hardware
  watchdog reset it; no such failure is known on the layer's own path, which has run hundreds of
  times. The runner's install was tested only against a fake driver, where that class of failure
  cannot show. That labgrid has no RAUC strategy settles who writes the code, not who owns the
  procedure — and the layer already owned one.
- **An optional `install` hook**, run like `build` and `bundle`, with every hook handed the board's
  `user@address` from labgrid's env file. It keeps the install a layer command and needs no new
  fixture. Rejected: the runner still waits for the board to answer over ssh after the hook and
  hands the board's address to every hook, so it keeps a device step of its own; installing in the
  target suite's own session fixture is where pytest and labgrid put such setup.

## Consequences

- **Provenance is checked, not guaranteed.** The runner controls which commit is built and named;
  that the board booted it is the layer's first check, and a layer that omits the check loses it.
- **A failed install is the bench's failure, not the candidate's.** It happens in a fixture's setup,
  and a fixture erroring is the bench's: the timer stops and nothing is posted, even when the
  candidate's bundle was at fault.
- **A layer with a different update mechanism needs only its own fixture**, not a runner change. The
  runner's config and record carry the commit only, never a bundle path or hash.

**Premise that would reopen this:** the layer's own update procedure proving unreliable on the bench
where a shared one would not be. That labgrid lacks a RAUC strategy, or that an install could be
written generically, is not that premise: it is the reasoning the rejected alternative rested on.

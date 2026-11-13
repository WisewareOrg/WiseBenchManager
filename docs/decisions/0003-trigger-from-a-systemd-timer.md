# 0003 — Start `bench run` from a systemd user timer under `flock`, not a CI runner

**Status:** accepted
**Decided:** 2026-10-11 (the runner survey that founded this repository; built in WisewareOrg/WiseBenchManager#6 `bench run`)
**Rev:** 3

## Revisions

- **rev 3** — 2026-11-13 — the timer is turned off with `systemctl --user stop`, never `disable`, and
  `bench status --watch`'s `:disable` only stops the timer (WisewareOrg/WiseBenchManager#24 `:enable`
  and `:disable`); the decision is unchanged.
- **rev 2** — 2026-11-12 — a tick takes a run queued by hand before the merge queue's head, and
  the lock is the tick's alone (ADR 0005 rev 1; #60 Queue behaviour); the decision is unchanged.
- **rev 1** — 2026-10-29 — first written (WisewareOrg/WiseBenchManager PR #31 ADR 0001 as trade studies).

## Context

The merge queue's head is gated by a run on the one physical board, from the build host beside it.
Something has to notice that the queue has an entry, start a run for it, keep a second run off the
board while one is going, and get the verdict back to GitHub. A run queued by hand for an
investigation (ADR 0005 rev 2) goes through the same trigger and the same exclusion.

GitHub has a mechanism built for exactly this, and it is the obvious answer: a self-hosted Actions
runner listening for `merge_group`. It would select the entry, serialise jobs and report status
with no code of ours. Pulling the other way, it is a long-lived registered service on the build
host. What the choice is held to:

- Organisation policy forbids a long-lived registered CI runner on the build host.
- A maintained tool before code of our own, for the trigger and the mutual exclusion both.
- A hand run and a queued run never share the board.

## Decision

**A systemd user timer starts `bench run`; an `fcntl.flock` keeps the board to one run.**

- Each tick takes the first entry of the queue of ADR 0005 rev 2: the oldest run queued by hand, else
  the merge queue's head, read from the `gh-readonly-queue` refs, taking the entry whose base is
  `main`'s current tip. With none, it exits.
- `bench run` holds one lock file beside the records for the whole of a run. A tick that finds it
  held backs off.
- Reading the merge queue's head is `Queue.head()` inside the one `Queue` class, so a different merge
  queue is a different `head()`.

## Alternatives considered

- **A self-hosted GitHub Actions runner on `merge_group`.** GitHub's own mechanism: entry selection,
  job serialisation and status reporting all for free, and no queue-reading code of ours. Rejected:
  it is a long-lived registered runner on the build host, which organisation policy forbids, and the
  timer and the lock already give the trigger and the exclusion with tools the host runs anyway.

- **`stop`, not `disable`.** The timer is installed as a link to the checkout's unit file, so
  `disable` deletes that link and `start` by name then fails with "Unit does not exist". Rejected:
  re-linking by path before every start, and installing the unit by copy. `stop` on `<unit>.timer`
  leaves a run in progress (the `.service`) alone.
- **`:disable` only stops the timer; it does not write `DISABLED`.** `DISABLED` means the bench
  faulted, not that it was stopped. Rejected: also writing `DISABLED` with a reason such as
  "disabled by hand" (owner ruling, 2026-11-13).

## Consequences

- **A queue entry, merge-queue or queued by hand, waits for the next tick** before a run picks it
  up, and for the current job too if the lock is held.
- **Queue-head selection is our code**, a `git ls-remote` read kept behind one class, where an
  Actions runner would have done it for free.
- **The bench stops itself.** The timer is the unit the bench's own failure stops, so a broken
  bench does not keep failing on every tick; restarting it is a hand step.
- **A hand stop does not persist.** A user-manager restart or a reboot re-arms the timer. After a
  fault the re-armed timer starts nothing, because every tick refuses on `DISABLED`.

**Premise that would reopen this:** the organisation's CI policy changing what may run on the build
host, which makes the Actions runner the pre-analysed alternative.

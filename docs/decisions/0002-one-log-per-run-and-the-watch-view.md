# 0002 — One log per run, and the watch view that reads it

**Status:** accepted
**Decided:** 2026-10-28 (WisewareOrg/WiseBenchManager#22 bench status: one log per run, and the
Run and History views)
**Rev:** 1

## Revisions

- **rev 1** — 2026-10-28 — first written (#22 bench status: one log per run, and the Run and
  History views).

## Context

The runner keeps no record of a run as a whole. Each pytest suite writes its own
`records/<timestamp>/` (ADR 0001 rev 2 decision 5), with no stage boundaries and nothing tying
`host_suite`'s record to `target_suite`'s as the same run; the `precondition`, `build` and
`bundle` hooks write no record at all, though the epic plan behind `bench status --watch`
(ADR 0001 decision 8) said run output would be streamed and kept. `bench status --watch` needs,
for a live run, its current stage and elapsed time, the build's progress, the test running now,
and recent verdicts, and, for history, each past run's outcome and log — none of which exists on
disk today in any form a view could read without the runner writing something new.

## Decision

1. **Record layout:** `records/<stamp>/{bench.log, host_suite/, target_suite/}` — one stamped
   directory per run (not per suite), holding `bench.log` (the run's whole output from the
   moment the job is taken, merged stdout and stderr, kept by coreutils `tee -i -a`) and the two
   suites' own `report.jsonl`/`run.json`, each pointed into a subdirectory of the run's own
   directory by a new environment variable, `BENCH_RECORD_DIR`, set per suite subprocess only.
   `bench.log` is kept output, not a new record format of this runner's own -- it answers ADR
   0001 rev 2's rejection of "a bespoke record writer": nothing here parses or defines
   `bench.log`'s structure beyond the marker lines within it, and pytest-reportlog's own report
   stays the record of test outcomes (ADR 0001 decision 5 holds, unchanged).
2. **Marker-line grammar**, one definition (writer and parser) in `src/wisebench/marks.py`:
   plain lines written to fd 1 (unbuffered, via `os.write`, tolerant of a closed reader),
   interleaved with whatever the hooks and pytest themselves write. A marker gives the view a
   position in the stream it has no other way to find -- a run's start, each stage's start and
   result, a suite's collected count after deselection, each test's start, and the setup of a
   fixture depending on `bench_candidate` -- and nothing else; test outcomes stay
   pytest-reportlog's.
3. **The operator's view** (`bench status --watch`, a later PR into this same ticket) is built
   on the standard library's `curses`.
4. **Hand runs get the same treatment as a service run:** a `RunLog`, markers, a kept
   `bench.log`, plain output -- splicing fd 1/2 through `tee` loses bitbake's live display and
   pytest's own colour in a terminal, and merges stderr into stdout, exactly what the systemd
   service already sees with no override.
5. **`contrib/bench-run.service` gets `KillMode=mixed`**, and `tee` runs as `tee -i`, so a
   `systemctl stop` or a `TimeoutStartSec` kill cannot take the logger down before the run's own
   error path finishes writing its last line to `bench.log`.

## Alternatives considered

- **`rich` or `textual` for the view.** Rejected: a new dependency for one operator's terminal
  view; the standard library's `curses` is already present and enough for a bordered, two-pane,
  vim-keyed screen.
- **Journal-only: the view reads `journalctl` instead of a kept file.** Rejected: a hand
  `bench test` has no journal entry at all, and the journal's own retention and rotation are
  outside this runner's control; a file beside the records the runner already owns is retained
  for exactly as long as those records are, by whatever retains them.
- **A new structured per-run record (events as JSON, a small database, and similar).**
  Rejected, again: ADR 0001 rev 2 already rejected "a bespoke record writer" for the test
  record, for the same reason this rejects one for the run record -- `bench.log` is kept process
  output with marker lines inside it, not a serialisation this runner defines and must keep a
  reader for.
- **One `report.jsonl`/`run.json` pair per `bench run`/`bench test` invocation, merging both
  suites.** Rejected: pytest-reportlog's format is per pytest session; reusing it as-is means two
  suites keep two pairs, as today. `bench.log` and its markers are what ties the two suites
  together as one run, not a merged record format.

## Consequences

- The runner writes to `bench.log` from the moment a job is taken (or a hand `bench test`
  starts) through its last marker line; a no-job cycle writes nothing and creates no run
  directory, so `records/` still grows by one entry per attempted run, not per cycle.
- `bench status --watch` can be built read-only: it never writes to `records/`, `DISABLED`, or
  any lock, and finds a live run by its missing `run <result>` line and a live pid, not by
  anything the runner tells it directly.
- ADR 0001 rev 7 points decisions 5 and 8 at this ADR: decision 5's record is this layout's
  suite-level part, unchanged; decision 8's "runner's own event stream" is superseded by
  `bench.log` and its markers -- the event stream decision 8 named was never built.
- A layer gains nothing new to know about: `BENCH_RECORD_DIR` is set only on the two suite
  subprocesses, the same way `BENCH_RESULT_DIR` already is.

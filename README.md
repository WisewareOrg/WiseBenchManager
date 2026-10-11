# WiseBenchManager

A generic pytest hardware-in-the-loop runner for over-the-air updated boards. It takes a git ref of a
Yocto layer through build, OTA install, boot, tests on the booted board, rollback and a verdict, and
it knows nothing about the application the board runs. Design decisions are in
[`docs/decisions/`](docs/decisions/README.md); the shape of the runner is ADR 0001 rev 1.

## Two commands

```sh
bench run                      # on a timer: if the merge queue has an entry, build it, OTA it to the
                               # board, run the suites, roll back, report pass or fail to GitHub;
                               # if the bench itself broke, post nothing and stop the timer
bench test <ref> [--set <name>] [-- <pytest args>]
                               # by hand, for investigations: the same path, results on the terminal;
                               # --set names a replay set for tests that use one
```

`bench test` resolves the ref to a commit, refuses a dirty tree, builds from that commit, installs what
it built, and the first target-suite test asserts the booted board's `/etc/buildinfo` names that commit.
There is no way to skip the build or supply an artefact: a run cannot test what it did not build from a
ref it can name. Arguments after `--` reach pytest unchanged; the record lists the tests that ran, the
requirement items they discharged and the exact command line.

## The job

1. `precondition` hook — the bench is in a known state. If a marker from a run that died mid-OTA is
   present, the runner asks RAUC which slot booted, marks the candidate bad and reboots if it is not the
   recorded good slot, verifies, clears the marker and continues.
2. `build` and `bundle` hooks — the layer's own commands, run in the checkout of the ref under test.
3. `host_suite` — pytest on the pipeline host against the built artefacts, before anything is installed.
4. Install and boot — the `ota` fixture: put the bundle, `rauc install`, reboot, assert the other slot
   booted, reboot once more.
5. `target_suite` — pytest against the booted board.
6. Roll back — the `ota` fixture's finalizer: mark the candidate slot bad, reboot, assert the previous
   slot is back. It runs whether the tests passed or not.
7. Verdict — a GitHub check run and comment (`bench run`), or the terminal (`bench test`).

A failing hook or test is the candidate's failure and is posted. An error in a fixture or the precondition
is the bench's failure: a DISABLED marker is written, the timer is stopped, nothing is posted.

## What a layer provides

The runner is pointed at a checkout and reads `bench.toml` from it:

```toml
schema = 1

[board]
address  = "${BENCH_ADDRESS}"     # from the environment, never committed
hostname = "${BENCH_HOSTNAME}"

[bundle]
path = "build/.../update-bundle-<machine>.raucb"

[hooks]
precondition = "just bench-precondition"
build        = "just build"
bundle       = "just bundle"

[tests]
host_suite   = "tests/host_suite"
target_suite = "tests/target_suite"

[github]
check_name = "bench"
```

A hook is a command run with the checkout as its working directory and `BENCH_SHA`, `BENCH_TREE`,
`BENCH_TARGET` and `BENCH_RESULT_DIR` in its environment; its exit status is pass or fail and its output
is the log. The suites are ordinary pytest directories. Tests ask for the runner's fixtures by name:

| Fixture | Gives |
|---|---|
| `board` | ssh `run`, `put`, `get`, `forward(port)`, `reboot()` |
| `ota` | session-scoped; the installed, booted candidate; its finalizer is the rollback |
| `proxy(har)` | mitmproxy serving that HAR file on a forwarded loopback port; a miss kills the connection; the file's hash goes into the record |

Everything that names the application under test — its units, its paths on the board, its page state, its
replay sets — lives in the layer's own `conftest.py` and tests, built on those fixtures. The runner never
learns it.

A test carries the requirement item it discharges as a marker: `@pytest.mark.tst("TST011")`. Passing
tests record their items; the record lists them.

## The record

One flat JSON file per run, plus one line appended to a JSON-lines index for bulk processing: the ref
and its commit, the runner's version (the tag the clone is at), the bundle hash, the booted slot before
and after, the hash of any replay recording used, every test's outcome, the items discharged and the
exact command line. Each fact is recorded by the fixture or test that learns it, through pytest's
`record_property`, and the file is written at session end, so a run that fails halfway still leaves
everything up to that point.

## Operating it

The runner is cloned on the pipeline host and run from its checkout at a tag; the record names the tag.
`bench run` is started by a systemd user timer under `flock`, so a second start while a job is running
exits without touching the board. `bench status --watch` shows the queue head, the phase strip with
elapsed times, build progress, the test running now, the replay window's state and recent verdicts, read
from the runner's own event stream. Replay recordings are made by hand with `bench record <set>`, the only
time the proxy reaches a real upstream.

## Status

Decided, not yet implemented. ADR 0001 is the design; implementation follows it in small PRs, each
proven on a real board before the next.

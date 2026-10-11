# WiseBenchManager

A generic pytest hardware-in-the-loop runner for over-the-air updated boards. It takes a git ref of a
Yocto layer through build, OTA install, boot, tests on the booted board, rollback and a verdict, and
it knows nothing about the application the board runs. Design decisions are in
[`docs/decisions/`](docs/decisions/README.md); the shape of the runner is ADR 0001 rev 2.

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
ref it can name. Arguments after `--` reach pytest unchanged; the record lists the tests that ran,
whatever a layer's own tests recorded against them, and the exact command line.

## The job

1. `precondition` hook — the bench is in a known state. One `ota.Ota(driver, marker)` is
   constructed per run; `Ota.recover()` checks the marker a prior `install` wrote before dying
   mid-OTA: if the board is still booted on the candidate slot the marker names, it marks that
   slot bad and reboots off it; either way it removes the marker before continuing.
2. `build` and `bundle` hooks — the layer's own commands, run in the checkout of the ref under test.
3. `host_suite` — pytest on the pipeline host against the built artefacts, before anything is installed.
4. Install and boot — `Ota.install(bundle, destination)`: reads the board's slots, records the
   booted and candidate slots, writes the marker, then labgrid's `put`, `rauc install`, reboot,
   wait for ssh to answer again; returns the booted slot and the bundle's sha256.
5. `target_suite` — pytest against the booted board, with labgrid's own env file and
   `--report-log`.
6. Roll back — `Ota.rollback()`, called whether the tests passed or not: `rauc status mark-bad`
   on the candidate `install` recorded, reboot, wait, assert the board is back on the slot
   `install` recorded as booted, clear the marker, and return the booted slot.
7. Verdict — a commit status and PR comment naming the verdict and the report path (`bench run`), or
   the terminal (`bench test`).

The same `Ota` instance's sequence is `recover()`, `install(...)`, `rollback()` — `recover`
first, so a run that died mid-OTA last time is undone before this run installs anything new.
`install` and `rollback` are plain methods the `bench test` script calls directly; neither is a
pytest fixture. A failing hook or test is the candidate's failure and is posted. An error in a
bench step — the precondition, an install or a rollback — is the bench's failure: a `DISABLED`
marker is written, the timer is stopped, nothing is posted.

## What a layer provides

The runner is pointed at a checkout and reads `bench.toml` from it. Board identity is never in it —
see "Running it":

```toml
schema = 1

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
`BENCH_RESULT_DIR` and `BENCH_SET` in its environment; its exit status is pass or fail and its output
is the log. The suites are ordinary pytest directories.

The board comes from labgrid's own pytest plugin and env file — a target named `main`, a
`NetworkService` resource with the address templated from the environment, an `SSHDriver` — pointed
at with `--lg-env`; the runner adds no fixture of its own for it. The one fixture the runner does
add:

| Fixture | Gives |
|---|---|
| `proxy(har)` | mitmproxy serving that HAR file on a forwarded loopback port; a miss kills the connection; the file's hash goes into the record |

Everything that names the application under test — its units, its paths on the board, its page state, its
replay sets — lives in the layer's own `conftest.py` and tests, built on labgrid's fixtures and the
runner's `proxy`. The runner never learns it; a test attaches its own facts, including any
requirement item it discharges, through pytest's `record_property`, which pytest-reportlog writes.
There is no marker and no credit rule of the runner's own.

## The record

pytest-reportlog's JSON-lines report (`--report-log`, set to `$BENCH_RESULT_DIR/<UTC start
time>/report.jsonl`; `BENCH_RESULT_DIR` defaults to `records/`) is the record: every test report,
with whatever a layer attaches through `record_property`. Beside it the runner writes one file per
run, `run.json`, written at session end so a run that fails halfway still leaves everything up to
that point: the runner's own version (`importlib.metadata.version("wisebench")`), the pytest command
line, the start and end times, and the exit status. OTA adds the bundle's hash and the booted slot
before and after; `bench test` adds the ref, its commit and the replay set name; `bench run` adds the
verdict. A per-item credit map is not built.

## Running it

```sh
git clone https://github.com/WisewareOrg/WiseBenchManager.git
git -C WiseBenchManager checkout <tag>
uv sync --project WiseBenchManager --frozen
```

`uv sync --frozen` installs exactly the versions in `uv.lock`. The package's version is derived from
the checked-out tag by hatch-vcs when uv builds it; `run.json` carries that version.

Three facts from labgrid 26.0 are the contract between this runner and the layer's env file:

- The file's path comes from `--lg-env <path>` or the `LG_ENV` variable
  (`labgrid/pytestplugin/hooks.py`); no `-p` flag of ours is needed alongside it.
- `!template` substitutes only `LG_`-prefixed environment variables (`labgrid/config.py`), so the
  board's address is named `LG_BENCH_ADDRESS`:

  ```yaml
  targets:
    main:
      resources:
        NetworkService:
          address: !template $LG_BENCH_ADDRESS
          username: root
      drivers:
        SSHDriver: {}
  ```

- The `target` fixture requires a target named `main` in that file
  (`labgrid/pytestplugin/fixtures.py`); a config without one raises `UserError`.

That env file, and everything else naming this board, lives in the layer, not here.

The runner is cloned on the pipeline host and run from its checkout at a tag; the record names the
tag. `bench run` is started by a systemd user timer under `flock`, so a second start while a job is
running exits without touching the board; it asks a queue adapter for the head entry (the GitHub
merge queue, via `gh api graphql`) and posts a commit status and PR comment with the verdict.
`bench status --watch`, on `rich`, reads `run.json`, `report.jsonl` and the hook logs: the queue
head, the timer and lock state, the current phase and its elapsed time, the build's progress tail,
the test running now, and recent verdicts. Replay recordings are made by hand with
`bench record <har-path>`, the only time the proxy reaches a real upstream.

# WiseBenchManager

A generic pytest hardware-in-the-loop runner for over-the-air updated boards. It takes a git ref of a
Yocto layer through build and test to a verdict, and it knows nothing about the application the board
runs — installing a candidate on the board is the layer's own job, through its own fixture. Design
decisions are in [`docs/decisions/`](docs/decisions/README.md).

## Four commands

```sh
bench run                      # on a timer: take one entry -- the oldest run queued by hand, else the
                               # merge queue's head -- build it, run the suites (the layer's own
                               # fixture installs it on the board); a merge-queue entry's pass or
                               # fail is reported to GitHub, a run queued by hand posts nothing; if
                               # the bench itself broke, post nothing and stop the timer
bench test <ref> [--set <name>] [--skip-build] [-- <pytest args>]
                               # by hand, for investigations: queue a run of <ref> and return; the
                               # next `bench run` takes it; --set names a replay set for tests that
                               # use one; --skip-build tests the board as it already is, with no
                               # build and no install
bench unqueue <id>             # remove a run queued by hand that `bench run` has not taken yet
bench status --watch           # read-only: the run in progress and every run on disk
```

`bench test` runs nothing. It resolves the ref to a commit in the bench checkout, queues the run in
`records/queue.db`, and prints `queued <id> <sha> place <n>`: the id for `bench unqueue`, the commit
the run will build, and its place among the runs queued by hand. A ref that does not resolve to a
commit exits 2 and queues nothing. `bench run` takes the oldest run queued by hand before the merge
queue's head, one entry per start; to run one now, start `bench run` by hand. A run queued by hand
posts nothing, and its record reads `kind=test ref=<as typed>`. Once taken, an entry is deleted: it
cannot be removed, and it is not queued again if its run ends in the bench's failure.

`bench test` does not fetch: it resolves the ref in the bench checkout, which refreshes `origin/*` on
every idle tick. For a branch pushed since, run `git fetch origin <branch>` there first. Queue from
the bench checkout -- the directory `bench run`'s service runs in -- with the service's own
`BENCH_RESULT_DIR` if its environment file sets one: the entry is written to that directory's
`records/queue.db`, and an entry queued anywhere else is never taken.

A queued run builds the commit resolved when it was queued -- unless `--skip-build`, which skips the
build, `bundle` and the host suite entirely -- and refuses a dirty tree when it starts. A built run's
target suite gets that commit as the `bench_candidate` fixture's value; a layer's own fixture installs
it on the board, and the first target-suite test asserts the booted board's `/etc/buildinfo` names that
commit. A `--skip-build` run's target suite gets `bench_candidate` `None` instead, and runs against the
board as it already is; its record carries an explicit `skip_build` field, and such a run is never
evidence for a review or an investigation, both of which need a build. A merge-queue run never skips
the build. Arguments after `--` reach pytest unchanged; the record lists the tests that ran, whatever a
layer's own tests recorded against them, and the exact command line.

## The job

1. `precondition` hook — the bench is in a known state; the layer's own command, run first.
2. `build` and `bundle` hooks — the layer's own commands, run in the checkout of the ref under test.
3. `host_suite` — pytest on the pipeline host against the built artefacts, before anything is installed.
4. `target_suite` — pytest against the board, with labgrid's own env file and the built commit as the
   `bench_candidate` fixture's value. A layer's own session fixture reads it and installs that commit on
   the board, through the layer's own update procedure, before any test in the suite runs.
5. Verdict — for a merge-queue entry, one GitHub check run via `Check.post`, naming the verdict and
   the report path; for a run queued by hand, the record alone.

`--skip-build` skips steps 2 and 3 entirely; step 4 then gets `bench_candidate` `None`.

A failing hook or test is the candidate's failure and, for a merge-queue entry, is posted. An error in a fixture or the
precondition is the bench's failure: a `DISABLED` marker is written, the timer is stopped,
nothing is posted. pytest's own setup/call distinction is what tells the two apart: a fixture
erroring during setup or teardown is never read as the candidate's test failing.

## What a layer provides

The runner is pointed at a checkout and reads `bench.toml` from it. Board identity is never in it —
see "Running it".

```toml
schema = 1

[hooks]
precondition = "just bench-precondition"
build        = "just build"
bundle       = "just bundle"

[tests]
host_suite   = "tests/host_suite"
target_suite = "tests/target_suite"

[github]
repo        = "owner/name"
app_id      = 123456
private_key = "/path/outside/the/tree/app.pem"
check       = "bench"

[run]
unit        = "bench-run"  # bench run disables bench-run.timer on the bench's own failure
```

`[github]` names the GitHub App `bench run` (issue #6) posts a verdict through: `repo` the
layer's own repository, `app_id` the App's id, `private_key` a filesystem path to the App's
PEM-encoded private key outside any tree (`~` is expanded), `check` the check run's name,
exactly as the layer's ruleset lists it as a required check. A run queued by hand does not read
`[github]`; a `bench.toml` without the table is fine for it. `Check`, built only for a merge-queue
entry, raises if any of the four is missing.

For a merge-queue entry, `bench run` calls:

```python
Check(repo, app_id, private_key, check).post(sha, conclusion, title, summary)
```

creating one completed check run on `sha` with `conclusion` `"success"` or `"failure"`, `title`
the one-line verdict and `summary` the report directory path.

### Setting the GitHub App up

A one-time, by-hand step, before `[github]` can be filled in:

1. Organization Settings → Developer settings → GitHub Apps → New GitHub App. Name it. Homepage
   URL: the runner's repository. Webhook: untick Active.
2. Repository permissions: Checks = Read and write. Nothing else (Metadata read is automatic).
3. Where can this app be installed: Only on this account. Create.
4. General page: note the App ID. Generate a private key. Move the downloaded PEM to a path
   outside any tree, e.g. `~/.config/wisebench/app.pem`, mode 600.
5. Install the App on the organization: Only select repositories, the layer's repository.
6. In the layer's repository: Settings → Rules → Rulesets → the main ruleset → Require status
   checks to pass → add the check's name. After the first check run has been posted, edit that
   entry and set its source to the App, so only the App can satisfy it.
7. Write `[github]` `repo`, `app_id`, `private_key` and `check` into `bench.toml`.

A hook is a command run with the checkout as its working directory and `BENCH_SHA`, `BENCH_TREE`,
`BENCH_SET` and `BENCH_RESULT_DIR` in its environment, output appended to that run's `bench.log`;
its exit status is pass or fail. The suites are ordinary pytest directories, run under the
runner's own interpreter, so a layer's test helpers may import only labgrid, pytest,
pytest-reportlog, tenacity, mitmproxy, the standard library and whatever the layer's own
`pythonpath` adds.
The checkout's own `pyproject.toml` `addopts` never reach that run; its `testpaths`, `pythonpath`
and markers still do.

The board comes from labgrid's own pytest plugin and env file — a target named `main`, a
`NetworkService` resource with the address templated from the environment, an `SSHDriver` — pointed
at with `--lg-env`; the runner adds no fixture of its own for the board itself. The fixtures the
runner does add:

| Fixture | Gives |
|---|---|
| `proxy(har)` | mitmproxy serving that HAR file on a forwarded loopback port, yielded as a `Proxy` whose `port`, `served()` and `killed()` a test reads; a miss kills the connection, is listed by `killed()`, and never ends the session; the file's hash and the served and killed counts go into the record |
| `bench_candidate` | the commit this run built, as a session fixture; `None` outside the runner. A layer's own target-suite fixture reads it and installs that commit on the board through the layer's own update procedure. |

Everything that names the application under test — its units, its paths on the board, its page state, its
replay sets — lives in the layer's own `conftest.py` and tests, built on labgrid's fixtures and the
runner's `proxy`. The runner never learns it; a test attaches its own facts, including any
requirement item it discharges, through pytest's `record_property`, which pytest-reportlog writes.
There is no pytest mark and no credit rule of the runner's own.

## The record

A run writes one folder, `records/<stamp>/`. It holds `bench.log` and one folder per suite,
`host_suite/` and `target_suite/`; each suite folder holds that suite's `report.jsonl` and
`run.json`. The runner sets `BENCH_RECORD_DIR` to the suite's folder, and the plugin writes there
directly. Run on its own, outside the runner, the plugin writes `$BENCH_RESULT_DIR/<UTC start
time>/` instead (`BENCH_RESULT_DIR` defaults to `records/`) and prints no `bench:` line.

`bench.log` holds every hook's and suite's output, and every test's output, passing or failing:
the suites run with `--capture=tee-sys` and `PYTHONUNBUFFERED=1`, so a line is in the log as it is
printed. The runner's own lines start `bench: <ISO time> ` and sit on their own line:

- `run started kind=<test|run> ref=<ref>`, with ` pr=<n>` (the PR its queue entry names) for a
  merge-queue run;
- `stage <name> started`, then `passed`, `failed` or `error`, for `checkout`, `precondition`,
  `build`, `bundle`, `host_suite`, `target_suite` and, when the result is posted, `posted`;
  `stage <name> skipped` for `build`, `bundle` and `host_suite` of a `--skip-build` run;
- inside a suite, `test queued <nodeid>` once per test it will run, once collection ends;
  `setup <fixture> started` when a fixture that takes `bench_candidate` is set up, named as the
  layer named it; and `test started <nodeid>` when a test's body begins. `<nodeid>` is pytest's
  own node id, relative to pytest's rootdir and not normalised by the runner;
- `run passed`, `run failed` or `run error`.

A section runs to the next marker. Test outcomes are not marked: they are in `report.jsonl`.

pytest-reportlog's JSON-lines report is the record of the tests: every test report, with
whatever a layer attaches through `record_property`. pytest-reportlog writes a test only when the
test ends and names no fixture, and pytest's own output has neither timestamps nor setup names. No
maintained tool writes these markers, so the plugin does.

Each suite's `run.json` is written at session end, so a run that fails halfway still leaves
everything up to that point: the runner's own version (`importlib.metadata.version("wisebench")`), the pytest command
line, the start and end times, and the exit status. The plugin reads whichever of a set of
`BENCH_*` variables the runner set are present and writes each into `run.json` under its own
field name: `test_commit` from `BENCH_SHA`, `set` from `BENCH_SET`, `skip_build` from
`BENCH_SKIP_BUILD` -- present only for a `--skip-build` run, so its presence is itself the mark of
one. `BENCH_TREE` (the checkout path) is set for every hook and suite but stays a hook-only variable;
it never reaches `run.json`. `bench test` itself never opens one. `bench run` adds the verdict.
A per-item credit map is not built. The `proxy` fixture attaches the recording's sha256 through
`record_property` under the key `recording_sha256`, and writes mitmdump's combined stdout and
stderr to `proxy-<test>-<n>.log` beside that suite's `report.jsonl`, one file per fixture use, with
the flow file `served()` and `killed()` read back at `proxy-<test>-<n>.flows` beside it. At
teardown it attaches `served_count` and `killed_count` through `record_property`.

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
tag. `bench run` is started by a systemd user timer and holds a `fcntl.flock` on a `lock` file beside the
records for the whole of a run; it backs off with exit 0 if the lock is already held. It asks
`Queue.take()` for one entry: the oldest run queued by hand, else `Queue.head()`'s merge queue head
entry -- the `gh-readonly-queue` ref whose own base is `origin/main`'s current tip, read with
`git ls-remote` -- whose verdict it posts as a GitHub check run (`Check.post`). A waiting run queued
by hand starts only when that read of the merge queue succeeds: a `git fetch` or `ls-remote` fault,
or more than one qualifying ref, writes `DISABLED` as on any tick, before the hand entry runs. `bench status --watch` is the read-only view of all
of it, run from the directory `bench run` runs in. It is a curses screen refreshed once a second:
Run shows the newest run's stages and the selected stage's log, headed by bitbake's task progress
while a build runs, and History lists every run on disk. It reads `records/*/bench.log`, each
suite's `report.jsonl`, whether `records/DISABLED` exists, the holder of the `lock` file from
`/proc/locks`, the run timer's state from `systemctl --user`, and `bench.toml`. It writes nothing,
takes no lock and runs no git. No maintained viewer reads the runner's markers or joins them to
pytest-reportlog's outcomes, so the view is the runner's own, on stdlib `curses`. Bitbake shows its
task progress only on the terminal that ran the build, so the view reads bitbake's `NOTE:` lines
from the log; and it asks the kernel's `/proc/locks` who holds the lock rather than `lslocks`,
which resolves holders by path through `/proc/<pid>/fd`.

### Installing `bench run`

In this clone, edit `contrib/bench-run.service`'s `WorkingDirectory` (the layer checkout),
`ExecStart` (this clone's own `.venv/bin/bench`) and `TimeoutStartSec` (comfortably longer than
one build-and-test cycle -- `RuntimeMaxSec` has no effect on a `Type=oneshot` service;
`TimeoutStartSec` is what bounds one); rename both `contrib/bench-run.service` and
`contrib/bench-run.timer` to `<unit>.service`/`<unit>.timer` if `bench.toml`'s `[run] unit` is
not `bench-run`. Write `LG_ENV` and `LG_BENCH_ADDRESS` into the path `EnvironmentFile` names.
Link and start both units straight from this checkout: `systemctl --user enable --now <this
clone>/contrib/<unit>.service <this clone>/contrib/<unit>.timer`.

A run that hits its own failure, or that `TimeoutStartSec` kills with `SIGTERM`, writes
`DISABLED` beside the records and disables that timer. Clearing it is a by-hand step, not
something `bench run` does itself: read `DISABLED`, remove it, then `systemctl --user enable
--now <unit>.timer` again. The run that faulted is not queued again; queue it again after clearing
`DISABLED`. If the reason is a database error, remove `records/queue.db` as well; the runs queued by
hand in it are lost.

Replay recordings are made by hand, the only time a proxy reaches a real upstream:

```sh
mitmdump --listen-host 127.0.0.1 --listen-port <port> --set hardump=<path>.har
```

Point a client at the running `mitmdump` as its HTTP(S) proxy and exercise whatever the set
should hold; what a recording keeps or drops is decided then, by whoever is recording, not by
this runner. mitmproxy generates its own CA at `~/.mitmproxy/mitmproxy-ca-cert.pem` on first
start; installing that CA on the board, so the board trusts the certificates the `proxy`
fixture's `mitmdump` presents, is the layer's precondition, not something this runner does.

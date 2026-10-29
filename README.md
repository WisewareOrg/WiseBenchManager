# WiseBenchManager

A generic pytest hardware-in-the-loop runner for over-the-air updated boards. It takes a git ref of a
Yocto layer through build and test to a verdict, and it knows nothing about the application the board
runs — installing a candidate on the board is the layer's own job, through its own fixture. Design
decisions are in [`docs/decisions/`](docs/decisions/README.md); the shape of the runner is ADR 0001 rev 7
and ADR 0002 rev 1.

## Two commands

```sh
bench run                      # on a timer: if the merge queue has an entry, build it, run the
                               # suites (the layer's own fixture installs it on the board), report
                               # pass or fail to GitHub; if the bench itself broke, post nothing
                               # and stop the timer
bench test <ref> [--set <name>] [--skip-build] [-- <pytest args>]
                               # by hand, for investigations: the same path, results on the terminal;
                               # --set names a replay set for tests that use one; --skip-build tests
                               # the board as it already is, with no build and no install
```

`bench test` resolves the ref to a commit and refuses a dirty tree, then builds from that commit --
unless `--skip-build`, which skips the build, `bundle` and the host suite entirely. A built run's
target suite gets that commit as the `bench_candidate` fixture's value; a layer's own fixture installs
it on the board, and the first target-suite test asserts the booted board's `/etc/buildinfo` names that
commit. A `--skip-build` run's target suite gets `bench_candidate` `None` instead, and runs against the
board as it already is; its record carries an explicit `skip_build` field, and such a run is never
evidence for a review or an investigation, both of which need a build. `bench run` never skips the
build. Arguments after `--` reach pytest unchanged; the record lists the tests that ran, whatever a
layer's own tests recorded against them, and the exact command line.

## The job

1. `precondition` hook — the bench is in a known state; the layer's own command, run first.
2. `build` and `bundle` hooks — the layer's own commands, run in the checkout of the ref under test.
3. `host_suite` — pytest on the pipeline host against the built artefacts, before anything is installed.
4. `target_suite` — pytest against the board, with labgrid's own env file and the built commit as the
   `bench_candidate` fixture's value. A layer's own session fixture reads it and installs that commit on
   the board, through the layer's own update procedure, before any test in the suite runs.
5. Verdict — one GitHub check run via `Check.post`, naming the verdict and the report path
   (`bench run`), or the terminal (`bench test`).

`--skip-build` skips steps 2 and 3 entirely; step 4 then gets `bench_candidate` `None`.

A failing hook or test is the candidate's failure and is posted. An error in a fixture or the
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
exactly as the layer's ruleset lists it as a required check. `bench test` does not read
`[github]`; a `bench.toml` without the table is fine for it. `Check`, built only by `bench run`,
raises if any of the four is missing.

`bench run` calls:

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
`BENCH_SET` and `BENCH_RESULT_DIR` in its environment, output inherited straight to the terminal;
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
There is no marker and no credit rule of the runner's own.

## The record

Every run (a `bench run` cycle that took a job, or any `bench test`) gets one stamped directory,
`$BENCH_RESULT_DIR/<UTC start time>/` (`BENCH_RESULT_DIR` defaults to `records/`; a second run in
the same second gets `-2`, then `-3`); a cycle that found no job creates none. It holds
`bench.log`, the run's whole output from the moment the job is taken -- both hooks and both
suites, stdout and stderr merged, captured by a real `tee` so the same bytes still reach the
terminal or the service's journal -- and `host_suite/`, `target_suite/`, each the matching suite's
own pytest-reportlog report and `run.json`, pointed there through `BENCH_RECORD_DIR`, set for
that suite subprocess only. `bench.log` carries `bench:`-prefixed marker lines (`src/wisebench/
marks.py`; the grammar is ADR 0002 rev 1) giving a reader positions in the stream -- a stage's
start and result, a suite's collected count, each test's start -- interleaved with whatever the
hooks and pytest themselves print; it is kept output, not a record format of its own, and test
outcomes stay pytest-reportlog's.

Within a suite's own directory, pytest-reportlog's JSON-lines report (`report.jsonl`) is the
record of every test report, with whatever a layer attaches through `record_property`. Beside it
the plugin writes `run.json` at session end, so a run that fails halfway still leaves everything
up to that point: the runner's own version (`importlib.metadata.version("wisebench")`), the pytest
command line, the start and end times, and the exit status. The plugin reads whichever of a set of
`BENCH_*` variables `bench test` set are present and writes each into `run.json` under its own
field name: `test_commit` from `BENCH_SHA`, `set` from `BENCH_SET`, `skip_build` from
`BENCH_SKIP_BUILD` -- present only for a `--skip-build` run, so its presence is itself the mark of
one. `BENCH_TREE` (the checkout path) is set for every hook and suite but stays a hook-only
variable; it never reaches `run.json`. A plain `pytest` invocation outside the runner, with
neither `BENCH_RECORD_DIR` nor `BENCH_RESULT_DIR` set, still writes its own `records/<timestamp>/`
this way, unchanged. A per-item credit map is not built. The `proxy` fixture attaches the
recording's sha256 through `record_property` under the key `recording_sha256`, and writes
mitmdump's combined stdout and stderr to `proxy-<test>-<n>.log` beside that suite's own
`report.jsonl`, one file per fixture use, with the flow file `served()` and `killed()` read back
at `proxy-<test>-<n>.flows` beside it. At teardown it attaches `served_count` and `killed_count`
through `record_property`.

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
tag. `bench run` is started by a systemd user timer; the lock it shares with `bench test` (a
`fcntl.flock` on a `lock` file beside the records) means a hand `bench test` started during a job
refuses rather than clashing with it, and `bench run` itself backs off with exit 0 if the lock is
already held. It asks `Queue.head()` for the merge queue's head entry -- the `gh-readonly-queue`
ref whose own base is `origin/main`'s current tip, read with `git ls-remote` -- and posts the
verdict as a GitHub check run (`Check.post`). `bench status --watch`, on `rich`, reads `run.json`,
`report.jsonl` and the hook logs: the queue head, the timer and lock state, the current phase and
its elapsed time, the build's progress tail, the test running now, and recent verdicts.

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
--now <unit>.timer` again.

Replay recordings are made by hand, the only time a proxy reaches a real upstream:

```sh
mitmdump --listen-host 127.0.0.1 --listen-port <port> --set hardump=<path>.har
```

Point a client at the running `mitmdump` as its HTTP(S) proxy and exercise whatever the set
should hold; what a recording keeps or drops is decided then, by whoever is recording, not by
this runner. mitmproxy generates its own CA at `~/.mitmproxy/mitmproxy-ca-cert.pem` on first
start; installing that CA on the board, so the board trusts the certificates the `proxy`
fixture's `mitmdump` presents, is the layer's precondition, not something this runner does.

# Operating the runner

How the runner is installed on the pipeline host, what it reads from the environment, what it writes,
and how a release is tagged. What the runner is and the layer contract are in the
[README](../README.md); the design is ADR 0001 rev 1.

## Install at a tag

The runner runs from its own clone, checked out at a tag; every record names that tag.

```sh
git clone https://github.com/WisewareOrg/WiseBenchManager.git
git -C WiseBenchManager checkout <tag>
uv sync --project WiseBenchManager --frozen
```

`uv sync --frozen` installs exactly the versions in `uv.lock`, and a Python of at least 3.11 if the host
has none. The package's version is derived from the checked-out tag by hatch-vcs when uv builds it, and
uv rebuilds it whenever the commit or the tags change; the record carries that version. The clone must stay
clean: the runner refuses to start when `git status --porcelain --untracked-files=no` lists a change.

## Run a layer's tests

From the layer's checkout, the directory holding its `bench.toml`:

```sh
uv run --project /path/to/WiseBenchManager pytest tests/target_suite
```

The runner's pytest plugin loads by itself. The first time a test asks for `board`, it reads
`bench.toml` from the checkout `BENCH_TREE` names, or from the directory pytest is started in when
`BENCH_TREE` is not set. A run like this never requests the `ota` fixture, so it
records `bench.ota_ran = false`, credits no requirement item and carries no verdict.

## Environment

| Variable | Read by | Meaning |
|---|---|---|
| `BENCH_ADDRESS` | the plugin | The board's ssh address, as `[user@]host[:port]`; a root login is written `root@host`; with no user, the ssh config's `User` applies, else the local user name |
| `BENCH_HOSTNAME` | the plugin | The hostname the board must answer with before any other command |
| `BENCH_TREE` | the plugin | The checkout holding `bench.toml`; default the directory pytest is started in |
| `BENCH_RESULT_DIR` | the plugin | Where records and the event stream are written; default `records/` under the start directory |

The board's address and hostname are read from these variables only: a `bench.toml` naming either one
is refused, and either one unset is the bench's failure.

The board is reached through labgrid's `SSHDriver`, which runs the host's own OpenSSH client over one
ControlMaster connection: authentication is ssh's, with the host user's keys, agent and `~/.ssh/config`,
and the `none` method where the board accepts it. labgrid does not check or save host keys, so the
board's identity is the hostname check: every connection the runner activates, including each one while
it waits out a reboot, answers `hostname` before any other command, and a wrong answer is the bench's
failure. labgrid's master sends an ssh keepalive every 15 s. `board.run` times out after 120 s unless the
test passes `timeout=`; a timeout or a lost connection is the bench's failure.

## What a run writes

In `BENCH_RESULT_DIR`:

- `<UTC start time>.json` — one flat record per session: the `bench.*` session facts, the exit status,
  the verdict, and every test's node id, outcome, recorded properties, item id and status.
- `index.jsonl` — one line per session: start time, record file, runner version, verdict, outcome counts.
- `events.jsonl` — one line per event (`session_start`, `test_start`, `test_end`, `record_written`), each
  with a UTC `ts`; the status view and the terminal read it.

## The unit

This version ships no systemd unit. `bench run` and the user timer that starts it under `flock` are
WisewareOrg/WiseBenchManager issue #6 `bench run`, which adds the unit templates and their install step
here.

## Tagging a release

Each merged runner PR gets one annotated version tag (`v<major>.<minor>.<patch>`, the form hatch-vcs
reads) on its merge commit, pushed by the person who merged it:

```sh
git fetch origin
git tag -a <tag> origin/main -m "<PR title>"
git push origin <tag>
```

The pipeline host then checks the new tag out in its clone. A commit past a tag, or before any tag,
records hatch-vcs's development form instead, such as `0.2.1.dev3+g<hash>`.

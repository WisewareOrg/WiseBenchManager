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
has none. The clone must stay clean: the runner reads its version with `git describe --tags --always
--dirty` and refuses to start when the result ends in `-dirty`.

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
is refused, and either one unset is the bench's failure. ssh authentication is the host
user's own: the keys `ssh` would offer, from `~/.ssh/config`, the agent and `~/.ssh/id_*`.
Authentication follows OpenSSH's order: key, then none. The board's identity is the hostname check;
host keys are not pinned, because a reflash changes them, and `known_hosts` is neither read nor written.
Every connection the runner opens, including each one while it waits out a reboot, is checked against
`BENCH_HOSTNAME` before any other command, and a connection the board closed is never reopened behind
that check. A command on the board times out after 120 s unless the test passes `timeout=`, and the
connection sends an ssh keepalive every 15 s; a timeout, like a dropped connection, is the bench's
failure.

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

Each merged runner PR gets one annotated tag on its merge commit, pushed by the person who merged it:

```sh
git fetch origin
git tag -a <tag> origin/main -m "<PR title>"
git push origin <tag>
```

The pipeline host then checks the new tag out in its clone. A host running a commit with no tag records
the `git describe` form instead (`<last tag>-<commits since>-g<hash>`, or the bare hash before any tag).

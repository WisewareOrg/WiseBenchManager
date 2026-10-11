# WiseBenchManager

A generic pytest hardware-in-the-loop runner for over-the-air updated boards. It takes a git ref of a
Yocto layer through build, OTA install, boot, tests on the booted board, rollback and a verdict, and
it knows nothing about the application the board runs. Design decisions are in
[`docs/decisions/`](docs/decisions/README.md); the shape of the runner is ADR 0001 rev 2.

## Two commands

`bench run` and `bench test <ref>` are a later ticket; this scope ships the pytest plugin underneath
them and nothing that invokes it.

## The job

The build, install, boot, test and rollback sequence ADR 0001 describes is a later ticket; this scope
ships only the plugin's own record.

## What a layer provides

The runner adds no fixture, no class, no guard, no marker, no credit rule, no event stream, no config
file and no board facts. The board is labgrid's own pytest plugin, pointed at an env file in the
layer with the address templated from the environment — see
[`docs/operating.md`](docs/operating.md). Everything about the board under test is attached by the
layer's own `conftest.py` and tests, through pytest's `record_property`, which pytest-reportlog
writes.

## The record

pytest-reportlog's JSON-lines report (`--report-log`, set to `$BENCH_RESULT_DIR/<UTC start
time>/report.jsonl`) is the record: every test report, with whatever a layer attaches through
`record_property`. Beside it the runner writes one file per run, `run.json`: its own version
(`importlib.metadata.version("wisebench")`), the pytest command line, the start and end times, and
the exit status. Nothing else; a verdict and a per-item credit map are a later ticket.

## Operating it

Installing, the environment and the labgrid env file are in [`docs/operating.md`](docs/operating.md).

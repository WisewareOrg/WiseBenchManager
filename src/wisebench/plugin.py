"""Turns on pytest-reportlog's record, and writes the one run-level file beside it."""

import json
import shlex
from datetime import UTC, datetime
from importlib.metadata import version
from os import environ
from pathlib import Path

import pytest

from wisebench.proxy import proxy as proxy

_TIMESTAMP = "%Y%m%dT%H%M%SZ"
DEFAULT_RESULT_DIR = "records"

# Environment variable names a caller (bench_test.py's BenchTest.run) and this plugin both use,
# defined once here so neither side carries its own copy of the literal string.
BENCH_RESULT_DIR = "BENCH_RESULT_DIR"
BENCH_SHA = "BENCH_SHA"
BENCH_TREE = "BENCH_TREE"
BENCH_SET = "BENCH_SET"
BENCH_SKIP_BUILD = "BENCH_SKIP_BUILD"

# The command-line option name bench_test.py's BenchTest.run and this plugin both use to pass
# the bench_candidate fixture's value, defined once here for the same reason.
BENCH_CANDIDATE_OPTION = "--bench-candidate"

# run.json field name -> the environment variable it comes from, when that fact is known.
# BENCH_SHA/SET are set for every hook and suite; BENCH_SKIP_BUILD only for a --skip-build run,
# so its presence is itself the record of one -- a bare None bench_candidate is not a run.json
# field and so cannot carry that signal on its own.
BENCH_FIELDS = {
    "image_commit": BENCH_SHA,
    "set": BENCH_SET,
    "skip_build": BENCH_SKIP_BUILD,
}


def _run_dir(config):
    base = Path(environ.get(BENCH_RESULT_DIR, DEFAULT_RESULT_DIR))
    return base / config._wisebench_timestamp


def pytest_addoption(parser):
    parser.addoption(BENCH_CANDIDATE_OPTION, default=None)


@pytest.fixture(scope="session")
def bench_candidate(request):
    """The commit this run built, or `None` without one.

    A layer's own session fixture reads this to install the candidate on the board through its
    own update procedure; the runner never touches the board itself.
    """
    return request.config.getoption(BENCH_CANDIDATE_OPTION)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    config._wisebench_timestamp = datetime.now(UTC).strftime(_TIMESTAMP)
    config._wisebench_start = datetime.now(UTC)
    config.option.report_log = str(_run_dir(config) / "report.jsonl")


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    run_dir = _run_dir(config)
    run_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "version": version("wisebench"),
        "command_line": shlex.join(config.invocation_params.args),
        "start": config._wisebench_start.isoformat(),
        "end": datetime.now(UTC).isoformat(),
        "exit_status": int(exitstatus),
    }
    for field, env_name in BENCH_FIELDS.items():
        value = environ.get(env_name)
        if value is not None:
            data[field] = value
    (run_dir / "run.json").write_text(json.dumps(data) + "\n")

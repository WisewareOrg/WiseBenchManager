"""pytest plugin: turns on pytest-reportlog's record, writes run.json, and provides
bench_candidate."""

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

# Environment variable names, shared with bench_test.py's BenchTest.run.
BENCH_RESULT_DIR = "BENCH_RESULT_DIR"
BENCH_SHA = "BENCH_SHA"
BENCH_TREE = "BENCH_TREE"
BENCH_SET = "BENCH_SET"
BENCH_SKIP_BUILD = "BENCH_SKIP_BUILD"

# Command-line option name, shared with bench_test.py's BenchTest.run.
BENCH_CANDIDATE_OPTION = "--bench-candidate"

# run.json field name -> environment variable; written only when the variable is set.
BENCH_FIELDS = {
    "test_commit": BENCH_SHA,
    "set": BENCH_SET,
    "skip_build": BENCH_SKIP_BUILD,
}

# Exit status substituted for pytest's own 1 when a fixture errors during setup or teardown,
# rather than a test's own call failing.
SETUP_ERROR_EXIT_STATUS = 90


def _run_dir(config):
    """The directory this run's records go in: BENCH_RESULT_DIR, then this run's timestamp.

    Args:
        config (pytest.Config): The session's pytest configuration.

    Returns:
        pathlib.Path: The run's own record directory.
    """
    base = Path(environ.get(BENCH_RESULT_DIR, DEFAULT_RESULT_DIR))
    return base / config._wisebench_timestamp


def pytest_addoption(parser):
    """Registers the --bench-candidate command-line option.

    Args:
        parser (pytest.Parser): pytest's own command-line parser.
    """
    parser.addoption(BENCH_CANDIDATE_OPTION, default=None)


@pytest.fixture(scope="session")
def bench_candidate(request):
    """The commit this run built, or `None` without one.

    A layer's own session fixture reads this to install the candidate on the board.

    Args:
        request (pytest.FixtureRequest): The requesting test's fixture request.

    Returns:
        str | None: The commit this run built from, or `None` outside a built run.
    """
    return request.config.getoption(BENCH_CANDIDATE_OPTION)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Sets this run's timestamp and points pytest-reportlog at this run's report path.

    Args:
        config (pytest.Config): The session's pytest configuration.
    """
    config._wisebench_timestamp = datetime.now(UTC).strftime(_TIMESTAMP)
    config._wisebench_start = datetime.now(UTC)
    config.option.report_log = str(_run_dir(config) / "report.jsonl")


def pytest_sessionfinish(session, exitstatus):
    """Writes run.json, after substituting exitstatus for a setup or teardown error.

    Args:
        session (pytest.Session): The finished session.
        exitstatus (int | pytest.ExitCode): pytest's own exit status for the session.
    """
    config = session.config
    terminalreporter = config.pluginmanager.get_plugin("terminalreporter")
    if terminalreporter is not None and terminalreporter.stats.get("error"):
        session.exitstatus = SETUP_ERROR_EXIT_STATUS
    run_dir = _run_dir(config)
    run_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "version": version("wisebench"),
        "command_line": shlex.join(config.invocation_params.args),
        "start": config._wisebench_start.isoformat(),
        "end": datetime.now(UTC).isoformat(),
        "exit_status": int(session.exitstatus),
    }
    for field, env_name in BENCH_FIELDS.items():
        value = environ.get(env_name)
        if value is not None:
            data[field] = value
    (run_dir / "run.json").write_text(json.dumps(data) + "\n")

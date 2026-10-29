"""pytest plugin: the `bench_candidate` and `proxy` fixtures, pytest-reportlog's record, and
run.json.

Each run writes `report.jsonl` and `run.json` under `<BENCH_RESULT_DIR>/<UTC timestamp>/`.
run.json holds `version`, `command_line`, `start`, `end`, `exit_status`, and each `BENCH_FIELDS`
field whose environment variable is set.
"""

import contextlib
import json
import shlex
from datetime import UTC, datetime
from importlib.metadata import version
from os import environ
from pathlib import Path

import pytest

from wisebench import marks
from wisebench.proxy import proxy as proxy

_TIMESTAMP = "%Y%m%dT%H%M%SZ"
DEFAULT_RESULT_DIR = "records"

# Environment variable names.
BENCH_RESULT_DIR = "BENCH_RESULT_DIR"
BENCH_RECORD_DIR = "BENCH_RECORD_DIR"
BENCH_SHA = "BENCH_SHA"
BENCH_TREE = "BENCH_TREE"
BENCH_SET = "BENCH_SET"
BENCH_SKIP_BUILD = "BENCH_SKIP_BUILD"

BENCH_CANDIDATE_OPTION = "--bench-candidate"

# run.json field name -> environment variable name.
BENCH_FIELDS = {
    "test_commit": BENCH_SHA,
    "set": BENCH_SET,
    "skip_build": BENCH_SKIP_BUILD,
}

# Exit status substituted for pytest's own when the session reports any error: a collection
# error, or a fixture erroring during setup or teardown.
SETUP_ERROR_EXIT_STATUS = 90


def _run_dir(config):
    """The directory this run's records go in.

    BENCH_RECORD_DIR, when set, names it directly -- the runner has already stamped a run
    directory for it, one level up. Otherwise BENCH_RESULT_DIR, then this run's own timestamp,
    as for a plain `pytest` invocation outside the runner.

    Args:
        config (pytest.Config): The session's pytest configuration.

    Returns:
        pathlib.Path: The run's own record directory.
    """
    record_dir = environ.get(BENCH_RECORD_DIR)
    if record_dir is not None:
        return Path(record_dir)
    base = Path(environ.get(BENCH_RESULT_DIR, DEFAULT_RESULT_DIR))
    return base / config._wisebench_timestamp


def _output_disabled(config):
    """A context manager suspending pytest's own output capture, the same way `-s` would.

    `BenchTest` runs each suite without `-s`; a marker written during a test's setup or call
    phase needs this, or pytest's own capture swallows it before it reaches fd 1 at all.

    Args:
        config (pytest.Config): The session's pytest configuration.

    Returns:
        AbstractContextManager: Suspends capture for its `with` block.
    """
    capman = config.pluginmanager.getplugin("capturemanager")
    if capman is None:
        return contextlib.nullcontext()
    return capman.global_and_fixture_disabled()


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


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items):
    """Prints `collected <n>`, counting the item set after any deselection, when
    BENCH_RECORD_DIR is set.

    Args:
        items (list[pytest.Item]): The collected items, after every other plugin's own
            `pytest_collection_modifyitems` has run.
    """
    if environ.get(BENCH_RECORD_DIR) is not None:
        marks.write_collected(len(items))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    """Prints `test started <nodeid>` at the start of the test's call phase when
    BENCH_RECORD_DIR is set.

    Args:
        item (pytest.Item): The test about to run.
    """
    if environ.get(BENCH_RECORD_DIR) is not None:
        with _output_disabled(item.config):
            marks.write_test_started(item.nodeid)
    return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    """Prints `setup <fixture> started` then `setup <fixture> <done|error>` around the setup of
    a fixture that depends on `bench_candidate`, when BENCH_RECORD_DIR is set.

    Args:
        fixturedef (_pytest.fixtures.FixtureDef): The fixture about to be set up.
        request (pytest.FixtureRequest): Unused; required by the hook's own signature.
    """
    tracked = environ.get(BENCH_RECORD_DIR) is not None and "bench_candidate" in fixturedef.argnames
    if tracked:
        with _output_disabled(request.config):
            marks.write_setup_started(fixturedef.argname)
    try:
        result = yield
    except BaseException:
        if tracked:
            with _output_disabled(request.config):
                marks.write_setup_result(fixturedef.argname, "error")
        raise
    if tracked:
        with _output_disabled(request.config):
            marks.write_setup_result(fixturedef.argname, "done")
    return result


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
    """Writes run.json, substituting SETUP_ERROR_EXIT_STATUS when the session reports any error.

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

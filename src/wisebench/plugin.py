"""Turns on pytest-reportlog's record, and writes the one run-level file beside it."""

import json
import shlex
from datetime import UTC, datetime
from importlib.metadata import version
from os import environ
from pathlib import Path

import pytest

_TIMESTAMP = "%Y%m%dT%H%M%SZ"


def _run_dir(config):
    base = Path(environ.get("BENCH_RESULT_DIR", "records"))
    return base / config._wisebench_timestamp


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
    (run_dir / "run.json").write_text(json.dumps(data) + "\n")

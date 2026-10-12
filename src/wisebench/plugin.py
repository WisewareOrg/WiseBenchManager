"""Turns on pytest-reportlog's record, and writes the one run-level file beside it."""

import json
import shlex
from datetime import UTC, datetime
from importlib.metadata import version
from os import environ
from pathlib import Path

import pytest

TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
DEFAULT_RESULT_DIR = "records"

# run.json field name -> the environment variable a caller (bench_test.py's BenchTest.run)
# sets it from, when that fact is known. BENCH_SHA/TREE/SET are set for every hook and suite;
# BENCH_BUNDLE_SHA256/SLOT_BEFORE/SLOT_AFTER only for the target suite, once install() has run.
BENCH_FIELDS = {
    "commit": "BENCH_SHA",
    "tree": "BENCH_TREE",
    "set": "BENCH_SET",
    "bundle_sha256": "BENCH_BUNDLE_SHA256",
    "slot_before": "BENCH_SLOT_BEFORE",
    "slot_after": "BENCH_SLOT_AFTER",
}


def _run_dir(config):
    base = Path(environ.get("BENCH_RESULT_DIR", DEFAULT_RESULT_DIR))
    return base / config._wisebench_timestamp


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    config._wisebench_timestamp = datetime.now(UTC).strftime(TIMESTAMP_FORMAT)
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

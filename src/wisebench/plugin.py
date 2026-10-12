"""Turns on pytest-reportlog's record, and writes the one run-level file beside it."""

import json
import shlex
from datetime import UTC, datetime
from importlib.metadata import version
from os import environ
from pathlib import Path

import pytest

_TIMESTAMP = "%Y%m%dT%H%M%SZ"
DEFAULT_RESULT_DIR = "records"

# Environment variable names a caller (bench_test.py's BenchTest.run) and this plugin both use,
# defined once here so neither side carries its own copy of the literal string.
BENCH_RESULT_DIR = "BENCH_RESULT_DIR"
BENCH_SHA = "BENCH_SHA"
BENCH_TREE = "BENCH_TREE"
BENCH_SET = "BENCH_SET"
BENCH_BUNDLE_SHA256 = "BENCH_BUNDLE_SHA256"

# run.json field name -> the environment variable it comes from, when that fact is known.
# BENCH_SHA/SET are set for every hook and suite; BENCH_BUNDLE_SHA256 only for the target
# suite, once install() has run. BENCH_TREE is a hook variable only -- it never reaches
# run.json.
BENCH_FIELDS = {
    "image_commit": BENCH_SHA,
    "set": BENCH_SET,
    "bundle_sha256": BENCH_BUNDLE_SHA256,
}


def _run_dir(config):
    base = Path(environ.get(BENCH_RESULT_DIR, DEFAULT_RESULT_DIR))
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
    for field, env_name in BENCH_FIELDS.items():
        value = environ.get(env_name)
        if value is not None:
            data[field] = value
    (run_dir / "run.json").write_text(json.dumps(data) + "\n")

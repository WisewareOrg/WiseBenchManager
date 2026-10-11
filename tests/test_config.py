from pathlib import Path

import pytest

from wisebench.config import Config
from wisebench.errors import BenchFailure

ENVIRON = {"BENCH_ADDRESS": "board.test", "BENCH_HOSTNAME": "bench-board"}

FULL = """
schema = 1

[board]
address = "${BENCH_ADDRESS}"
hostname = "${BENCH_HOSTNAME}"

[bundle]
path = "build/update.raucb"

[hooks]
precondition = "true"
build = "make build"
bundle = "make bundle"

[tests]
host_suite = "tests/host_suite"
target_suite = "tests/target_suite"

[github]
check_name = "bench"
"""


def write(root: Path, text: str) -> Path:
    (root / "bench.toml").write_text(text)
    return root


def test_reads_every_table_and_expands_variables(tmp_path: Path) -> None:
    config = Config.load(write(tmp_path, FULL), ENVIRON)
    assert config.root == tmp_path
    assert config.board.address == "board.test"
    assert config.board.hostname == "bench-board"
    assert config.bundle_path == "build/update.raucb"
    assert config.hooks.build == "make build"
    assert config.tests.target_suite == "tests/target_suite"
    assert config.github == {"check_name": "bench"}


def test_board_optional_keys_default(tmp_path: Path) -> None:
    board = Config.load(write(tmp_path, FULL), ENVIRON).board
    assert (board.name, board.reboot_timeout) == ("board", 180)


def test_board_optional_keys_are_read(tmp_path: Path) -> None:
    text = FULL.replace("[bundle]", 'name = "bench"\nreboot_timeout = 30\n\n[bundle]')
    board = Config.load(write(tmp_path, text), ENVIRON).board
    assert (board.name, board.reboot_timeout) == ("bench", 30)


def test_expansion_reaches_nested_tables_and_arrays(tmp_path: Path) -> None:
    text = FULL.replace('check_name = "bench"', 'labels = ["${BENCH_HOSTNAME}"]')
    assert Config.load(write(tmp_path, text), ENVIRON).github == {"labels": ["bench-board"]}


def test_missing_variable_is_named(tmp_path: Path) -> None:
    with pytest.raises(BenchFailure, match="BENCH_HOSTNAME is not set"):
        Config.load(write(tmp_path, FULL), {"BENCH_ADDRESS": "board.test"})


def test_variable_set_to_empty_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BenchFailure, match=r"\[board\] hostname must be a non-empty string"):
        Config.load(write(tmp_path, FULL), {**ENVIRON, "BENCH_HOSTNAME": ""})


def test_reads_the_process_environment_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in ENVIRON.items():
        monkeypatch.setenv(name, value)
    assert Config.load(write(tmp_path, FULL)).board.hostname == "bench-board"


@pytest.mark.parametrize("schema", ["2", "0", '"1"', "true", "1.0"])
def test_unknown_schema_is_refused(tmp_path: Path, schema: str) -> None:
    with pytest.raises(BenchFailure, match="schema"):
        Config.load(write(tmp_path, FULL.replace("schema = 1", f"schema = {schema}")), ENVIRON)


def test_missing_schema_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BenchFailure, match="schema None"):
        Config.load(write(tmp_path, FULL.replace("schema = 1", "")), ENVIRON)


def test_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BenchFailure, match="bench.toml not found"):
        Config.load(tmp_path, ENVIRON)


def test_malformed_toml_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BenchFailure, match="bench.toml"):
        Config.load(write(tmp_path, "schema = = 1"), ENVIRON)


@pytest.mark.parametrize("table", ["board", "bundle", "hooks", "tests", "github"])
def test_missing_table_is_refused(tmp_path: Path, table: str) -> None:
    text = FULL.replace(f"[{table}]", "[unrelated]")
    with pytest.raises(BenchFailure, match=rf"table \[{table}\] is missing"):
        Config.load(write(tmp_path, text), ENVIRON)


@pytest.mark.parametrize("key", ["address", "path", "build", "target_suite"])
def test_missing_key_is_refused(tmp_path: Path, key: str) -> None:
    text = "\n".join(line for line in FULL.splitlines() if not line.startswith(f"{key} "))
    with pytest.raises(BenchFailure, match=f"{key} must be a non-empty string"):
        Config.load(write(tmp_path, text), ENVIRON)


@pytest.mark.parametrize("value", ["0", "-5", '"180"', "true"])
def test_reboot_timeout_must_be_positive_number(tmp_path: Path, value: str) -> None:
    text = FULL.replace("[bundle]", f"reboot_timeout = {value}\n\n[bundle]")
    with pytest.raises(BenchFailure, match="reboot_timeout must be a positive number"):
        Config.load(write(tmp_path, text), ENVIRON)

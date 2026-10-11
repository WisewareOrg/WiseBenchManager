from pathlib import Path

import pytest

from wisebench.config import BoardConfig, Config
from wisebench.errors import BenchFailure

FULL = """
schema = 1

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


@pytest.fixture(autouse=True)
def board_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BENCH_ADDRESS", "root@board.test")
    monkeypatch.setenv("BENCH_HOSTNAME", "bench-board")


def write(root: Path, text: str) -> Path:
    (root / "bench.toml").write_text(text)
    return root


def test_reads_every_table_and_the_board_from_the_environment(tmp_path: Path) -> None:
    config = Config.load(write(tmp_path, FULL))
    assert config.board_config() == BoardConfig("root@board.test", "bench-board", "board", 180)
    assert config.toml.bundle.path == "build/update.raucb"
    assert config.toml.hooks.build == "make build"
    assert config.toml.tests.target_suite == "tests/target_suite"
    assert config.toml.github == {"check_name": "bench"}


def test_board_table_keys_are_read(tmp_path: Path) -> None:
    text = FULL + '\n[board]\nname = "bench"\nreboot_timeout = 30\n'
    board = Config.load(write(tmp_path, text)).board_config()
    assert (board.name, board.reboot_timeout) == ("bench", 30)


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    Config.load(write(tmp_path, FULL + "\n[later]\nkey = 1\n"))


@pytest.mark.parametrize("variable", ["BENCH_ADDRESS", "BENCH_HOSTNAME"])
def test_missing_board_variable_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    monkeypatch.delenv(variable)
    with pytest.raises(BenchFailure, match=f"{variable}: Field required"):
        Config.load(write(tmp_path, FULL))


def test_empty_board_variable_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BENCH_HOSTNAME", "")
    with pytest.raises(BenchFailure, match="BENCH_HOSTNAME: String should have at least 1"):
        Config.load(write(tmp_path, FULL))


@pytest.mark.parametrize(
    "key", ["BENCH_ADDRESS", "BENCH_HOSTNAME", "bench_hostname", "Bench_Address"]
)
@pytest.mark.parametrize("environment", ["set", "unset"])
def test_an_identity_key_in_the_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, environment: str
) -> None:
    if environment == "unset":
        monkeypatch.delenv("BENCH_ADDRESS")
        monkeypatch.delenv("BENCH_HOSTNAME")
    text = FULL.replace("schema = 1", f'schema = 1\n{key} = "root@committed.example"')
    with pytest.raises(BenchFailure, match=f"{key} is read from the environment only"):
        Config.load(write(tmp_path, text))


@pytest.mark.parametrize("name", ["hostname", "HOSTNAME", "address"])
def test_a_key_that_cannot_name_the_board_is_ignored(tmp_path: Path, name: str) -> None:
    text = FULL.replace("schema = 1", f'schema = 1\n{name} = "elsewhere"')
    assert Config.load(write(tmp_path, text)).board_config().hostname == "bench-board"


@pytest.mark.parametrize(
    ("table", "value"),
    [("github", '{"check_name": "from-env"}'), ("hooks", '{"build": "x"}'), ("schema", "1")],
)
def test_the_environment_does_not_fill_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, table: str, value: str
) -> None:
    monkeypatch.setenv(table.upper(), value)
    text = (
        FULL.replace("schema = 1", "") if table == "schema" else FULL.replace(f"[{table}]", "[x]")
    )
    with pytest.raises(BenchFailure, match=f"{table}: Field required"):
        Config.load(write(tmp_path, text))


@pytest.mark.parametrize("schema", ["2", "0", '"1"', "true", "1.0"])
def test_any_schema_but_1_is_refused(tmp_path: Path, schema: str) -> None:
    with pytest.raises(BenchFailure, match="schema: "):
        Config.load(write(tmp_path, FULL.replace("schema = 1", f"schema = {schema}")))


def test_missing_schema_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BenchFailure, match="schema: Field required"):
        Config.load(write(tmp_path, FULL.replace("schema = 1", "")))


def test_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BenchFailure, match="bench.toml not found"):
        Config.load(tmp_path)


def test_malformed_toml_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BenchFailure, match="bench.toml: Invalid value"):
        Config.load(write(tmp_path, "schema = = 1"))


@pytest.mark.parametrize("table", ["bundle", "hooks", "tests", "github"])
def test_missing_table_is_refused(tmp_path: Path, table: str) -> None:
    text = FULL.replace(f"[{table}]", "[unrelated]")
    with pytest.raises(BenchFailure, match=f"{table}: Field required"):
        Config.load(write(tmp_path, text))


@pytest.mark.parametrize(
    ("key", "where"),
    [("path", "bundle.path"), ("build", "hooks.build"), ("target_suite", "tests.target_suite")],
)
def test_missing_key_is_refused(tmp_path: Path, key: str, where: str) -> None:
    text = "\n".join(line for line in FULL.splitlines() if not line.startswith(f"{key} "))
    with pytest.raises(BenchFailure, match=f"{where}: Field required"):
        Config.load(write(tmp_path, text))


def test_empty_string_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BenchFailure, match="hooks.build: String should have at least 1"):
        Config.load(write(tmp_path, FULL.replace('build = "make build"', 'build = ""')))


@pytest.mark.parametrize("value", ["0", "-5", '"180"', "true"])
def test_reboot_timeout_must_be_a_positive_number(tmp_path: Path, value: str) -> None:
    text = FULL + f"\n[board]\nreboot_timeout = {value}\n"
    with pytest.raises(BenchFailure, match="board.reboot_timeout"):
        Config.load(write(tmp_path, text))

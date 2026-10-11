from pathlib import Path

import pytest
from conftest import BENCH_TOML

from wisebench.config import Config
from wisebench.errors import BenchFailure


@pytest.fixture(autouse=True)
def identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BENCH_ADDRESS", "root@board.test")
    monkeypatch.setenv("BENCH_HOSTNAME", "bench-board")


def load(root: Path, text: str = BENCH_TOML) -> Config:
    (root / "bench.toml").write_text(text)
    return Config.load(root)


def test_the_file_gives_the_tables_and_the_environment_the_board(tmp_path: Path) -> None:
    config = load(tmp_path, BENCH_TOML + "\n[later]\nunknown = 1\n")
    assert (config.identity.address, config.identity.hostname) == ("root@board.test", "bench-board")
    assert (config.toml.board.name, config.toml.board.reboot_timeout) == ("bench", 180)
    assert config.toml.hooks.build == "true"


@pytest.mark.parametrize("variable", ["BENCH_ADDRESS", "BENCH_HOSTNAME"])
def test_a_missing_identity_variable_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    monkeypatch.delenv(variable)
    with pytest.raises(BenchFailure, match=rf"{variable}\s+Field required"):
        load(tmp_path)


@pytest.mark.parametrize("key", ["BENCH_ADDRESS", "bench_hostname"])
@pytest.mark.parametrize("environment", ["set", "unset"])
def test_an_identity_key_in_the_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, environment: str
) -> None:
    if environment == "unset":
        monkeypatch.delenv("BENCH_ADDRESS")
        monkeypatch.delenv("BENCH_HOSTNAME")
    text = BENCH_TOML.replace("schema = 1", f'schema = 1\n{key} = "root@committed.example"')
    with pytest.raises(BenchFailure, match=f"{key} is read from the environment only"):
        load(tmp_path, text)


def test_the_environment_does_not_fill_a_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB", "{}")
    with pytest.raises(BenchFailure, match=r"github\s+Field required"):
        load(tmp_path, BENCH_TOML.replace("[github]", ""))


@pytest.mark.parametrize("schema", ["2", "true", "1.0", '"1"'])
def test_any_schema_but_1_is_refused(tmp_path: Path, schema: str) -> None:
    with pytest.raises(BenchFailure, match="schema"):
        load(tmp_path, BENCH_TOML.replace("schema = 1", f"schema = {schema}"))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("[hooks]", "[x]"), r"hooks\s+Field required"),
        (('build = "true"', 'build = ""'), r"hooks.build\s+String should have at least 1"),
        (('name = "bench"', "reboot_timeout = 0"), r"board.reboot_timeout"),
        (('name = "bench"', "reboot_timeout = true"), r"board.reboot_timeout"),
        (("schema = 1", "schema = = 1"), "bench.toml: Invalid"),
    ],
)
def test_a_missing_or_invalid_value_is_refused(
    tmp_path: Path, change: tuple[str, str], message: str
) -> None:
    with pytest.raises(BenchFailure, match=message):
        load(tmp_path, BENCH_TOML.replace(*change))


def test_a_checkout_without_bench_toml_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BenchFailure, match="bench.toml not found"):
        Config.load(tmp_path)

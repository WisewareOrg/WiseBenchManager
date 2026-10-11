"""The layer's `bench.toml`, read from a checkout root."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wisebench.errors import BenchFailure

FILENAME = "bench.toml"
SCHEMA = 1


class Expander:
    """Replaces every `${VAR}` in a string with the variable's value from an environment."""

    PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._environ = environ

    def expand(self, value: Any) -> Any:
        """Expand strings, recursing into tables; other values pass through."""
        if isinstance(value, str):
            return self.PATTERN.sub(self._lookup, value)
        if isinstance(value, dict):
            return {key: self.expand(item) for key, item in value.items()}
        return value

    def _lookup(self, match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in self._environ:
            raise BenchFailure(f"{FILENAME}: environment variable {name} is not set")
        return self._environ[name]


class Table:
    """One TOML table, read key by key with the file's name in every refusal."""

    def __init__(self, data: dict[str, Any], name: str) -> None:
        value = data.get(name)
        if not isinstance(value, dict):
            raise BenchFailure(f"{FILENAME}: table [{name}] is missing")
        self._data = value
        self._name = name

    def string(self, key: str, default: str | None = None) -> str:
        value = self._data.get(key, default)
        if not isinstance(value, str) or not value:
            raise BenchFailure(f"{FILENAME}: [{self._name}] {key} must be a non-empty string")
        return value

    def number(self, key: str, default: float) -> float:
        value = self._data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            raise BenchFailure(f"{FILENAME}: [{self._name}] {key} must be a positive number")
        return value

    def raw(self) -> dict[str, Any]:
        return dict(self._data)


@dataclass(frozen=True)
class BoardConfig:
    address: str
    hostname: str
    name: str
    reboot_timeout: float


@dataclass(frozen=True)
class HooksConfig:
    precondition: str
    build: str
    bundle: str


@dataclass(frozen=True)
class SuitesConfig:
    host_suite: str
    target_suite: str


@dataclass(frozen=True)
class Config:
    """Every value the layer's `bench.toml` gives the runner."""

    board: BoardConfig
    bundle_path: str
    hooks: HooksConfig
    tests: SuitesConfig
    github: dict[str, Any]

    @classmethod
    def load(cls, root: Path, environ: Mapping[str, str] | None = None) -> Config:
        """Read `root/bench.toml`; a missing file, variable or key, or another schema raises."""
        path = root / FILENAME
        try:
            with path.open("rb") as handle:
                raw = tomllib.load(handle)
        except FileNotFoundError:
            raise BenchFailure(f"{FILENAME} not found in {root}") from None
        except tomllib.TOMLDecodeError as error:
            raise BenchFailure(f"{FILENAME}: {error}") from None
        schema = raw.get("schema")
        if type(schema) is not int or schema != SCHEMA:
            raise BenchFailure(f"{FILENAME}: schema {schema!r} is not {SCHEMA}")
        data = Expander(os.environ if environ is None else environ).expand(raw)
        board = Table(data, "board")
        hooks = Table(data, "hooks")
        tests = Table(data, "tests")
        return cls(
            board=BoardConfig(
                address=board.string("address"),
                hostname=board.string("hostname"),
                name=board.string("name", "board"),
                reboot_timeout=board.number("reboot_timeout", 180),
            ),
            bundle_path=Table(data, "bundle").string("path"),
            hooks=HooksConfig(
                precondition=hooks.string("precondition"),
                build=hooks.string("build"),
                bundle=hooks.string("bundle"),
            ),
            tests=SuitesConfig(
                host_suite=tests.string("host_suite"),
                target_suite=tests.string("target_suite"),
            ),
            github=Table(data, "github").raw(),
        )

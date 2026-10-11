"""The layer's `bench.toml` and the board's identity from the environment, on pydantic-settings."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, Field, StrictFloat, StrictInt, StringConstraints, ValidationError
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from wisebench.errors import BenchFailure

FILENAME = "bench.toml"

Text = Annotated[str, StringConstraints(min_length=1)]
Seconds = Annotated[StrictInt | StrictFloat, Field(gt=0)]


@dataclass(frozen=True)
class BoardConfig:
    """What the board fixture needs: where the board is, who it must be, and its timeout."""

    address: str
    hostname: str
    name: str
    reboot_timeout: float


class BoardTable(BaseModel):
    name: Text = "board"
    reboot_timeout: Seconds = 180


class BundleTable(BaseModel):
    path: Text


class HooksTable(BaseModel):
    precondition: Text
    build: Text
    bundle: Text


class SuitesTable(BaseModel):
    host_suite: Text
    target_suite: Text


class Config(BaseSettings):
    """`bench.toml` under a checkout; `BENCH_ADDRESS` and `BENCH_HOSTNAME` from the environment."""

    model_config = SettingsConfigDict(extra="ignore")

    address: Text = Field(validation_alias="BENCH_ADDRESS")
    hostname: Text = Field(validation_alias="BENCH_HOSTNAME")
    schema_version: Annotated[StrictInt, Field(ge=1, le=1)] = Field(validation_alias="schema")
    board: BoardTable = BoardTable()
    bundle: BundleTable
    hooks: HooksTable
    tests: SuitesTable
    github: dict[str, Any]

    @classmethod
    def load(cls, root: Path) -> Config:
        """Read `root/bench.toml` and the environment; any missing or invalid value raises."""
        path = root / FILENAME
        if not path.is_file():
            raise BenchFailure(f"{FILENAME} not found in {root}")
        try:
            sources = (TomlConfigSettingsSource(cls, toml_file=path), EnvSettingsSource(cls))
            return cls(_build_sources=(sources, {}))
        except tomllib.TOMLDecodeError as error:
            raise BenchFailure(f"{FILENAME}: {error}") from None
        except ValidationError as error:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in problem['loc'])}: {problem['msg']}"
                for problem in error.errors()
            )
            raise BenchFailure(f"{FILENAME} and environment: {problems}") from None

    def board_config(self) -> BoardConfig:
        return BoardConfig(
            address=self.address,
            hostname=self.hostname,
            name=self.board.name,
            reboot_timeout=self.board.reboot_timeout,
        )

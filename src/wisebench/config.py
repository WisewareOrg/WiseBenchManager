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


IDENTITY_KEYS = ("BENCH_ADDRESS", "BENCH_HOSTNAME")


class Identity(BaseSettings):
    """The board's address and hostname, from the environment and nowhere else."""

    address: Text = Field(validation_alias="BENCH_ADDRESS")
    hostname: Text = Field(validation_alias="BENCH_HOSTNAME")

    @classmethod
    def read(cls) -> Identity:
        return cls(_build_sources=((EnvSettingsSource(cls),), {}))


class BenchToml(BaseSettings):
    """`bench.toml`, from the file and nowhere else; unknown keys are ignored."""

    model_config = SettingsConfigDict(extra="ignore")

    schema_version: Annotated[StrictInt, Field(ge=1, le=1)] = Field(validation_alias="schema")
    board: BoardTable = BoardTable()
    bundle: BundleTable
    hooks: HooksTable
    tests: SuitesTable
    github: dict[str, Any]

    @classmethod
    def read(cls, path: Path) -> BenchToml:
        """Read the file; a top-level key naming an identity variable, in any case, is refused."""
        source = TomlConfigSettingsSource(cls, toml_file=path)
        identity = {key.casefold() for key in IDENTITY_KEYS}
        for key in source.toml_data:
            if key.casefold() in identity:
                raise BenchFailure(f"{FILENAME}: {key} is read from the environment only")
        return cls(_build_sources=((source,), {}))


@dataclass(frozen=True)
class Config:
    """The checkout's `bench.toml` and the board's identity from the environment."""

    identity: Identity
    toml: BenchToml

    @classmethod
    def load(cls, root: Path) -> Config:
        """Read `root/bench.toml` and the environment; any missing or invalid value raises."""
        path = root / FILENAME
        if not path.is_file():
            raise BenchFailure(f"{FILENAME} not found in {root}")
        try:
            toml = BenchToml.read(path)
            return cls(identity=Identity.read(), toml=toml)
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
            address=self.identity.address,
            hostname=self.identity.hostname,
            name=self.toml.board.name,
            reboot_timeout=self.toml.board.reboot_timeout,
        )

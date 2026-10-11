"""The runner's own version, read from its clone with dunamai."""

from __future__ import annotations

from pathlib import Path

from dunamai import Version

from wisebench.errors import BenchFailure

ANY_TAG = r"^(?P<base>.+)$"


def describe(clone: Path) -> str:
    """The clone's version in `git describe --tags --always` form; uncommitted changes raise.

    A tagged commit is the tag, a later commit `<tag>-<distance>-g<hash>`, a clone with no tag the
    bare hash.
    """
    try:
        version = Version.from_git(path=clone, pattern=ANY_TAG, strict=True, ignore_untracked=True)
        described = version.base
        if version.distance:
            described = f"{version.base}-{version.distance}-g{version.commit}"
    except RuntimeError:
        try:
            version = Version.from_git(path=clone, pattern=ANY_TAG, ignore_untracked=True)
        except RuntimeError as error:
            raise BenchFailure(f"runner version unknown: {error}") from None
        described = version.commit
    if version.dirty:
        raise BenchFailure(
            f"the runner's clone has uncommitted changes ({described}); refusing to run"
        )
    return described


def runner_version() -> str:
    """The version of the clone this package runs from."""
    return describe(Path(__file__).resolve().parent)

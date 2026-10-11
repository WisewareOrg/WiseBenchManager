"""The runner's own version, read from its clone and serialised by dunamai."""

from __future__ import annotations

from pathlib import Path

from dunamai import Version

from wisebench.errors import BenchFailure

ANY_TAG = r"^(?P<base>.+)$"
AT_TAG = "{base}"
PAST_TAG = "{base}-{distance}-g{commit}"
NO_TAG = "{commit}"


def describe(clone: Path) -> str:
    """The clone's version in `git describe --tags --always` form; uncommitted changes raise.

    A tagged commit is the tag, a later commit `<tag>-<distance>-g<hash>`, a clone with no tag the
    bare hash.
    """
    try:
        version = Version.from_git(path=clone, pattern=ANY_TAG, strict=True, ignore_untracked=True)
        form = PAST_TAG if version.distance else AT_TAG
    except RuntimeError:
        try:
            version = Version.from_git(path=clone, pattern=ANY_TAG, ignore_untracked=True)
        except RuntimeError as error:
            raise BenchFailure(f"runner version unknown: {error}") from None
        form = NO_TAG
    described = version.serialize(format=form)
    if version.dirty:
        raise BenchFailure(
            f"the runner's clone has uncommitted changes ({described}); refusing to run"
        )
    return described


def runner_version() -> str:
    """The version of the clone this package runs from."""
    return describe(Path(__file__).resolve().parent)

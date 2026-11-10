"""Bench state: whether the newest run is executing, whether DISABLED exists, and the run timer.

Read only: no lock is taken, DISABLED is checked for presence, and the one process started is
`systemctl --user is-active`.
"""

import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from wisebench.config import Config

DASH = "—"


@dataclass(frozen=True)
class Bench:
    """What the bench itself is doing.

    Args:
        live (bool): The newest run is executing.
        disabled (bool): `DISABLED` exists in the records directory.
        timer (str): The run timer: "on", "off", or "—" when unknown.
    """

    live: bool
    disabled: bool
    timer: str


def read_bench(directory, newest, run_unit, proc=Path("/proc")):
    """Read the bench's state.

    Args:
        directory (pathlib.Path): The records directory.
        newest (Run | None): The newest run.
        run_unit (str | None): The systemd unit base name; None gives the timer "—".
        proc (pathlib.Path): The `/proc` directory.

    Returns:
        Bench: The bench's state.
    """
    return Bench(
        live=newest is not None and newest.result is None and lock_held(directory, proc),
        disabled=(Path(directory) / "DISABLED").exists(),
        timer=DASH if run_unit is None else timer_state(run_unit),
    )


def lock_held(directory, proc=Path("/proc")):
    """Whether some process holds a write `flock` on the records directory's `lock` file.

    Matched on the inode alone: `/proc/locks` prints the superblock's device, which need not be
    the file's `st_dev`.

    Args:
        directory (pathlib.Path): The records directory.
        proc (pathlib.Path): The `/proc` directory.

    Returns:
        bool: True if the lock is held.
    """
    try:
        inode = os.stat(Path(directory) / "lock").st_ino
        locks = (Path(proc) / "locks").read_text().splitlines()
    except OSError:
        return False
    for entry in locks:
        fields = entry.split()
        if len(fields) < 6 or fields[1] != "FLOCK" or fields[3] != "WRITE":
            continue
        if fields[5].rsplit(":", 1)[-1] == str(inode):
            return True
    return False


def timer_state(run_unit):
    """The run timer's state from `systemctl --user is-active <run_unit>.timer`.

    Args:
        run_unit (str): The systemd unit base name.

    Returns:
        str: "on" (exit 0), "off" (exit 3), or "—" for any other exit or a call that failed.
    """
    try:
        done = subprocess.run(
            ["systemctl", "--user", "is-active", f"{run_unit}.timer"],
            capture_output=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return DASH
    return {0: "on", 3: "off"}.get(done.returncode, DASH)


def read_run_unit(directory):
    """The `[run] unit` of `bench.toml` in `directory`; None when it is absent or does not load.

    Args:
        directory (pathlib.Path): The directory holding `bench.toml`.

    Returns:
        str | None: The unit base name.
    """
    try:
        return Config.load(Path(directory) / "bench.toml").run_unit
    except (OSError, tomllib.TOMLDecodeError, KeyError):
        return None

"""Bench state: whether a run is executing, whether the bench is disabled, the timer's state.

Reads files and asks `systemctl`; takes no lock and writes nothing.
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
        timer (str): "on", "off" or "—".
    """

    live: bool
    disabled: bool
    timer: str


def is_live(directory, run, proc_root):
    """Whether `run` is executing: it has no result line and a process holds a write `flock` on
    the records directory's `lock` file.

    The lock is matched by inode only; `/proc/locks` prints the superblock's device, which need
    not equal the file's `st_dev`. No lock is taken.

    Args:
        directory (pathlib.Path): The records directory.
        run (Run): The newest run.
        proc_root (pathlib.Path): The `/proc` directory.

    Returns:
        bool: True if the run is live.
    """
    if run.result is not None:
        return False
    try:
        inode = os.stat(Path(directory) / "lock").st_ino
        locks = (Path(proc_root) / "locks").read_text().splitlines()
    except OSError:
        return False
    for entry in locks:
        fields = entry.split()
        try:
            held = fields[1] == "FLOCK" and fields[3] == "WRITE"
            if held and int(fields[5].rsplit(":", 1)[1]) == inode:
                return True
        except (IndexError, ValueError):
            continue
    return False


def timer_state(run_unit):
    """The run timer's state from `systemctl --user is-active`.

    Args:
        run_unit (str): The systemd unit base name.

    Returns:
        str: "on" (exit 0), "off" (exit 3), or "—" for any other exit or a failed call.
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


def read_run_unit(cwd):
    """The `[run] unit` of `cwd/bench.toml`, or None when it is absent or does not load.

    Args:
        cwd (pathlib.Path): The directory holding `bench.toml`.

    Returns:
        str | None: The unit base name.
    """
    try:
        return Config.load(Path(cwd) / "bench.toml").run_unit
    except (OSError, tomllib.TOMLDecodeError, KeyError):
        return None


def read_bench(directory, newest, run_unit, proc_root):
    """Read whether the newest run is live, whether DISABLED exists, and the timer's state.

    Args:
        directory (pathlib.Path): The records directory; DISABLED is checked for presence only.
        newest (Run | None): The newest run.
        run_unit (str | None): The systemd unit base name; None gives the timer "—".
        proc_root (pathlib.Path): The `/proc` directory.

    Returns:
        Bench: The bench's state.
    """
    return Bench(
        live=newest is not None and is_live(directory, newest, proc_root),
        disabled=(Path(directory) / "DISABLED").exists(),
        timer=DASH if run_unit is None else timer_state(run_unit),
    )

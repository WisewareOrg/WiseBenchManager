"""Bench state: whether a run is executing, whether the bench is disabled, the timer's state.

It also changes the timer and `DISABLED`: `stop_timer` and `enable`.
"""

import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from wisebench.config import Config

DASH = "—"
DISABLED = "DISABLED"


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
    """Whether `run` is executing.

    It is when the run has no result line and a process holds a write `flock` on the records
    directory's `lock` file.

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
        done = _systemctl_run("is-active", run_unit)
    except (OSError, subprocess.SubprocessError):
        return DASH
    return {0: "on", 3: "off"}.get(done.returncode, DASH)


def _systemctl_run(verb, run_unit, **options):
    """Run `systemctl --user <verb> <run_unit>.timer`, capturing output, under the 1 s timeout."""
    argv = ["systemctl", "--user", verb, f"{run_unit}.timer"]
    return subprocess.run(argv, capture_output=True, timeout=1, **options)


def _systemctl(verb, run_unit):
    """Run `systemctl --user <verb> <run_unit>.timer`.

    Args:
        verb (str): "stop" or "start".
        run_unit (str): The systemd unit base name.

    Returns:
        str | None: None on exit 0, else `systemctl failed: <detail>` -- stderr, else the exit
            code, else the exception's text. Never raises.
    """
    try:
        done = _systemctl_run(verb, run_unit, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"systemctl failed: {exc}"
    if done.returncode == 0:
        return None
    return f"systemctl failed: {done.stderr.strip() or f'exit {done.returncode}'}"


def stop_timer(run_unit):
    """Stop the run timer; a run in progress is the `.service` and is left alone.

    Args:
        run_unit (str): The systemd unit base name.

    Returns:
        str | None: None on success, else a message-ready failure text.
    """
    return _systemctl("stop", run_unit)


def enable(directory, run_unit):
    """Remove `DISABLED` (absent is fine), then start the run timer.

    Args:
        directory (pathlib.Path): The records directory `DISABLED` sits in.
        run_unit (str): The systemd unit base name.

    Returns:
        str | None: None on success, else a message-ready failure text; the timer is not
            started when `DISABLED` cannot be removed.
    """
    try:
        (Path(directory) / DISABLED).unlink(missing_ok=True)
    except OSError as exc:
        return f"cannot remove DISABLED: {exc}"
    return _systemctl("start", run_unit)


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
        disabled=(Path(directory) / DISABLED).exists(),
        timer=DASH if run_unit is None else timer_state(run_unit),
    )

"""Bench state: whether a run is executing, whether the bench is disabled, the timer's state.

It also changes the timer and `DISABLED`: `stop_timer` and `enable`.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from wisebench.config import read_settings, settings_path

DASH = "—"
DISABLED = "DISABLED"


@dataclass(frozen=True)
class Bench:
    """What the bench itself is doing.

    Args:
        live (bool): The newest run is executing.
        disabled (bool): `DISABLED` exists in the records directory.
        timer (str): "on", "off" or "—".
        board_name (str | None): The added board's name; None for the first board.
        on (bool): The added board is turned on.
    """

    live: bool
    disabled: bool
    timer: str
    board_name: str | None = None
    on: bool = False


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


def _systemctl_run(verb, run_unit):
    """The `CompletedProcess` of `systemctl --user <verb> <run_unit>.timer`, text captured.

    Raises:
        OSError: `systemctl` could not be started.
        subprocess.SubprocessError: The call timed out after 1 s.
    """
    argv = ["systemctl", "--user", verb, f"{run_unit}.timer"]
    return subprocess.run(argv, capture_output=True, text=True, timeout=1)


def _change_timer(verb, run_unit):
    """Stop or start the run timer: None on success, else a message-ready failure text.

    Args:
        verb (str): "stop" or "start".
        run_unit (str): The systemd unit base name.

    Returns:
        str | None: None on exit 0, else `systemctl failed: <detail>` -- stderr, else the exit
            code, else the exception's text. Never raises.
    """
    try:
        done = _systemctl_run(verb, run_unit)
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
    return _change_timer("stop", run_unit)


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
    return _change_timer("start", run_unit)


def read_run_unit():
    """The `[run] unit` of the bench's settings file, or None when it is absent or does not load.

    Returns:
        str | None: The unit base name.
    """
    try:
        return read_settings(settings_path()).get("run", {}).get("unit")
    except (OSError, ValueError):
        return None


def read_bench(directory, newest, run_unit, proc_root, board=None):
    """Read whether the newest run is live, whether DISABLED exists, the timer and the switch.

    Args:
        directory (pathlib.Path): The records directory; DISABLED is checked for presence only.
        newest (Run | None): The newest run.
        run_unit (str | None): The systemd unit base name; None gives the timer "—".
        proc_root (pathlib.Path): The `/proc` directory.
        board (wisebench.board.Board | None): The added board; None for the first board.

    Returns:
        Bench: The bench's state.
    """
    return Bench(
        live=newest is not None and is_live(directory, newest, proc_root),
        disabled=(Path(directory) / DISABLED).exists(),
        timer=DASH if run_unit is None else timer_state(run_unit),
        board_name=board and board.name,
        on=board is not None and board.is_on(),
    )

"""OTA install, rollback and mid-OTA recovery over labgrid's SSHDriver and rauc.

The mid-OTA marker is a file at a caller-chosen path. Its content is the candidate slot's
name. The caller writes it (`write_marker`) before calling `install` and removes it
(`clear_marker`) after calling `rollback`; `recover` reads it to detect a run that died with
it still present.
"""

import hashlib
import json

from tenacity import Retrying, stop_after_delay, wait_fixed

_WAIT_TIMEOUT = 300
_WAIT_INTERVAL = 5


class OtaError(Exception):
    """A rauc command exited non-zero."""


def _run(driver, cmd):
    """Run a command on the board and raise on a nonzero exit.

    Args:
        driver: A labgrid SSHDriver (or stand-in) with a `run(cmd)` method returning
            `(stdout, stderr, returncode)`.
        cmd (str): The command line to run on the board.

    Returns:
        list[str]: The command's stdout, one line per element.

    Raises:
        OtaError: The command's exit code was nonzero.
    """
    stdout, stderr, returncode = driver.run(cmd)
    if returncode != 0:
        raise OtaError(f"{cmd!r} exited {returncode}: {stderr}")
    return stdout


def _booted_slot(driver):
    """Read the currently booted slot's name from the board.

    Args:
        driver: A labgrid SSHDriver (or stand-in) with a `run(cmd)` method.

    Returns:
        str: The booted slot's name (e.g. `rootfs.0`), from the `slots` array of `rauc
        status --output-format=json` -- the identifier `rauc status mark-bad`/`mark-good`
        accept, not the top-level `booted` field's bootname (`A`/`B`).

    Raises:
        OtaError: `rauc status` exited nonzero.
    """
    (line,) = _run(driver, "rauc status --output-format=json")
    slots = json.loads(line)["slots"]
    return next(name for slot in slots for name, info in slot.items() if info["state"] == "booted")


def _reconnect(driver):
    """Re-establish the SSH control master labgrid tears down on reboot.

    Args:
        driver: A labgrid SSHDriver (or stand-in) bound to a labgrid Target.

    Returns:
        None.

    Raises:
        Exception: labgrid raised while deactivating or reactivating the driver (including,
            but not limited to, `labgrid.driver.ExecutionError`).
    """
    driver.target.deactivate(driver)
    driver.target.activate(driver)


def _wait_for_ssh(driver, timeout=_WAIT_TIMEOUT, interval=_WAIT_INTERVAL):
    """Retry reconnecting and running a trivial command until ssh answers again.

    Args:
        driver: A labgrid SSHDriver (or stand-in) bound to a labgrid Target.
        timeout (float): Seconds to keep retrying before giving up.
        interval (float): Seconds to wait between retries.

    Returns:
        None.

    Raises:
        Exception: The last exception raised by reconnecting or running, once `timeout`
            elapses without ssh answering.
    """
    for attempt in Retrying(
        stop=stop_after_delay(timeout),
        wait=wait_fixed(interval),
        reraise=True,
    ):
        with attempt:
            _reconnect(driver)
            driver.run("true")


def install(driver, bundle, destination):
    """Put a bundle on the board, install it with rauc, and reboot into it.

    Args:
        driver: A labgrid SSHDriver (or stand-in) bound to a labgrid Target.
        bundle (str | os.PathLike): Path to the local `.raucb` bundle file.
        destination (str): Path on the board to copy the bundle to before installing it.

    Returns:
        tuple[str, str]: The booted slot's name (e.g. `rootfs.0`) after the reboot, and the
        bundle file's sha256 hex digest.

    Raises:
        OtaError: `rauc install` or the slot-status read exited nonzero.
        Exception: ssh did not answer again within the wait bound after rebooting.
    """
    with open(bundle, "rb") as f:
        sha256 = hashlib.file_digest(f, "sha256").hexdigest()
    driver.put(str(bundle), destination)
    _run(driver, f"rauc install {destination}")
    driver.run("reboot")
    _wait_for_ssh(driver)
    return _booted_slot(driver), sha256


def rollback(driver, slot):
    """Mark a slot bad and reboot off it.

    Args:
        driver: A labgrid SSHDriver (or stand-in) bound to a labgrid Target.
        slot (str): The slot's name (e.g. `rootfs.0`) to mark bad.

    Returns:
        str: The booted slot's name after the reboot.

    Raises:
        OtaError: `rauc status mark-bad` or the slot-status read exited nonzero.
        Exception: ssh did not answer again within the wait bound after rebooting.
    """
    _run(driver, f"rauc status mark-bad {slot}")
    driver.run("reboot")
    _wait_for_ssh(driver)
    return _booted_slot(driver)


def recover(driver, marker):
    """Undo a run that died between `install` and `rollback`.

    If `marker` exists and the board is still booted on the slot it names, mark that slot
    bad and reboot off it. Either way, remove `marker` once the board is safe.

    Args:
        driver: A labgrid SSHDriver (or stand-in) bound to a labgrid Target.
        marker (pathlib.Path): Path to the mid-OTA marker file.

    Returns:
        None.

    Raises:
        OtaError: `rauc status mark-bad` or a slot-status read exited nonzero.
        Exception: ssh did not answer again within the wait bound after rebooting.
    """
    if not marker.exists():
        return
    candidate = marker.read_text().strip()
    if _booted_slot(driver) == candidate:
        _run(driver, f"rauc status mark-bad {candidate}")
        driver.run("reboot")
        _wait_for_ssh(driver)
    marker.unlink()


def write_marker(marker, slot):
    """Write the mid-OTA marker file.

    Args:
        marker (pathlib.Path): Path to write the marker file to.
        slot (str): The candidate slot's name, written as the marker's content.

    Returns:
        None.
    """
    marker.write_text(slot)


def clear_marker(marker):
    """Remove the mid-OTA marker file, if present.

    Args:
        marker (pathlib.Path): Path to the marker file.

    Returns:
        None.
    """
    marker.unlink(missing_ok=True)

"""Install, rollback and mid-OTA recovery over labgrid's SSHDriver and rauc."""

import hashlib
import json

from labgrid.driver import ExecutionError
from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed

_WAIT_TIMEOUT = 180
_WAIT_INTERVAL = 5


class OtaError(Exception):
    """A rauc command exited non-zero."""


def _run(driver, cmd):
    stdout, stderr, returncode = driver.run(cmd)
    if returncode != 0:
        raise OtaError(f"{cmd!r} exited {returncode}: {stderr}")
    return stdout


def _booted_slot(driver):
    (line,) = _run(driver, "rauc status --output-format=json")
    return json.loads(line)["booted"]


def _reconnect(driver):
    """Re-establish the SSH control master labgrid tears down on reboot."""
    driver.target.deactivate(driver)
    driver.target.activate(driver)


def _wait_for_ssh(driver, timeout=_WAIT_TIMEOUT, interval=_WAIT_INTERVAL):
    for attempt in Retrying(
        retry=retry_if_exception_type(ExecutionError),
        stop=stop_after_delay(timeout),
        wait=wait_fixed(interval),
        reraise=True,
    ):
        with attempt:
            _reconnect(driver)
            driver.run("true")


def install(driver, bundle, destination):
    """Put `bundle` at `destination`, rauc install it, reboot, wait for ssh, and
    return (booted slot, bundle sha256)."""
    with open(bundle, "rb") as f:
        sha256 = hashlib.file_digest(f, "sha256").hexdigest()
    driver.put(str(bundle), destination)
    _run(driver, f"rauc install {destination}")
    driver.run("reboot")
    _wait_for_ssh(driver)
    return _booted_slot(driver), sha256


def rollback(driver, slot):
    """Mark `slot` bad, reboot, wait for ssh, and return the booted slot."""
    _run(driver, f"rauc status mark-bad {slot}")
    driver.run("reboot")
    _wait_for_ssh(driver)
    return _booted_slot(driver)


def recover(driver, marker):
    """If `marker` names the slot still booted, mark it bad and reboot; always clear `marker`."""
    if not marker.exists():
        return
    candidate = marker.read_text().strip()
    if _booted_slot(driver) == candidate:
        _run(driver, f"rauc status mark-bad {candidate}")
        driver.run("reboot")
        _wait_for_ssh(driver)
    marker.unlink()


def write_marker(marker, slot):
    marker.write_text(slot)


def clear_marker(marker):
    marker.unlink(missing_ok=True)

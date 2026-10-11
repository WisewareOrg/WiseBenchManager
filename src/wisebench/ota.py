"""OTA install, rollback and mid-OTA recovery over labgrid's SSHDriver and rauc."""

import hashlib
import json

from tenacity import Retrying, stop_after_delay, wait_fixed

_WAIT_TIMEOUT = 300
_WAIT_INTERVAL = 5


class OtaError(Exception):
    """A rauc command exited non-zero."""


class Ota:
    """Install, rollback and mid-OTA recovery for one board, over one labgrid SSHDriver.

    Holds the driver, the mid-OTA marker's path, and the ssh-wait bound; calls the driver,
    never re-exposing it. `install` records the booted slot and the candidate it is about to
    boot into, and writes the marker (the candidate's name) before rebooting; `rollback` and
    `recover` each remove the marker once the board is back on a known-good slot. `recover`,
    built fresh after a crash, reads the marker's own content for the candidate it never
    recorded locally.
    """

    def __init__(self, driver, marker, timeout=_WAIT_TIMEOUT, interval=_WAIT_INTERVAL):
        """Bind one Ota to a board and a marker file.

        Args:
            driver: A labgrid SSHDriver (or stand-in) bound to a labgrid Target.
            marker (pathlib.Path): Path to the mid-OTA marker file.
            timeout (float): Seconds to keep retrying an ssh reconnect before giving up.
            interval (float): Seconds to wait between ssh reconnect retries.
        """
        self._driver = driver
        self._marker = marker
        self._timeout = timeout
        self._interval = interval
        self._previous = None
        self._candidate = None

    def _slots(self):
        """Read the board's slots and name the booted one and the other.

        The single place that reads which slot is booted from `rauc status
        --output-format=json`; every other method goes through this one.

        Returns:
            tuple[str, str]: The booted slot's name (e.g. `rootfs.0`) and the other slot's
            name -- identifiers `rauc status mark-bad`/`mark-good` accept, not the top-level
            `booted` field's bootname (`A`/`B`).

        Raises:
            OtaError: `rauc status` exited nonzero.
        """
        stdout, stderr, returncode = self._driver.run("rauc status --output-format=json")
        if returncode != 0:
            raise OtaError(f"rauc status exited {returncode}: {stderr}")
        slots = json.loads(stdout[0])["slots"]
        booted = next(
            name for slot in slots for name, info in slot.items() if info["state"] == "booted"
        )
        other = next(
            name for slot in slots for name, info in slot.items() if info["state"] != "booted"
        )
        return booted, other

    def _wait_for_ssh(self):
        """Reconnect and retry a trivial command until ssh answers again or the bound elapses.

        Reconnecting re-establishes the SSH control master labgrid tears down on reboot
        (`driver.target.deactivate`/`.activate`); that alone only proves the master
        authenticated, not that a session is usable, since the keepalive is a separate,
        unchecked subprocess and every real command gates on it -- so each retry also runs a
        trivial command (`true`) as the actual proof.

        Raises:
            Exception: The last exception raised by reconnecting or running, once the bound
                timeout elapses without ssh answering.
        """
        for attempt in Retrying(
            stop=stop_after_delay(self._timeout),
            wait=wait_fixed(self._interval),
            reraise=True,
        ):
            with attempt:
                self._driver.target.deactivate(self._driver)
                self._driver.target.activate(self._driver)
                self._driver.run("true")

    def _mark_bad_and_verify(self, candidate, expected):
        """Mark a slot bad, reboot, wait, and confirm the board came back on the right slot.

        Used by both `rollback` and `recover`, so the crash path verifies exactly what the
        normal path verifies. `reboot`'s own exit status is not checked: it is unreliable
        once the board goes down.

        Args:
            candidate (str): The slot's name to mark bad.
            expected (str): The slot's name that must be booted once the board comes back.

        Returns:
            str: The booted slot's name (equal to `expected`).

        Raises:
            OtaError: `rauc status mark-bad` or a slot-status read exited nonzero, or the
                board came back on a slot other than `expected`.
            Exception: ssh did not answer again within the wait bound after rebooting.
        """
        _, stderr, returncode = self._driver.run(f"rauc status mark-bad {candidate}")
        if returncode != 0:
            raise OtaError(f"rauc status mark-bad exited {returncode}: {stderr}")
        self._driver.run("reboot")
        self._wait_for_ssh()
        booted, _ = self._slots()
        if booted != expected:
            raise OtaError(f"expected {expected} booted after mark-bad {candidate}, found {booted}")
        return booted

    def install(self, bundle, destination):
        """Put a bundle on the board, install it with rauc, and reboot into it.

        Records the booted slot and the candidate slot (the board's other slot) before
        installing, and writes the marker with the candidate's name before rebooting.
        `reboot`'s own exit status is not checked: it is unreliable once the board goes down.

        Args:
            bundle (str | os.PathLike): Path to the local `.raucb` bundle file.
            destination (str): Path on the board to copy the bundle to before installing it.

        Returns:
            tuple[str, str]: The booted slot's name (e.g. `rootfs.0`) after the reboot, and
            the bundle file's sha256 hex digest.

        Raises:
            OSError: `bundle` could not be opened to compute its sha256, or the marker file
                could not be written.
            OtaError: `rauc install` or a slot-status read exited nonzero.
            Exception: ssh did not answer again within the wait bound after rebooting.
        """
        self._previous, self._candidate = self._slots()
        self._marker.write_text(self._candidate)

        with open(bundle, "rb") as f:
            sha256 = hashlib.file_digest(f, "sha256").hexdigest()
        self._driver.put(str(bundle), destination)
        _, stderr, returncode = self._driver.run(f"rauc install {destination}")
        if returncode != 0:
            raise OtaError(f"rauc install exited {returncode}: {stderr}")
        self._driver.run("reboot")
        self._wait_for_ssh()
        booted, _ = self._slots()
        return booted, sha256

    def rollback(self):
        """Mark the candidate slot bad and reboot off it.

        Uses the candidate and previous slots `install` recorded; clears the marker once the
        board is confirmed back on the previous slot.

        Returns:
            str: The booted slot's name after the reboot.

        Raises:
            OtaError: `rauc status mark-bad` or a slot-status read exited nonzero, or the
                board did not come back on the previous slot.
            Exception: ssh did not answer again within the wait bound after rebooting.
            OSError: The marker file could not be removed.
        """
        booted = self._mark_bad_and_verify(self._candidate, self._previous)
        self._marker.unlink()
        return booted

    def recover(self):
        """Undo a run that died between `install` and `rollback`, then remove the marker.

        Reads the candidate from the marker file itself, not from `self._candidate`: a fresh
        `Ota` built to recover a crashed run never had its own `install` set that state. If
        the board is still booted on that candidate, the expected slot after mark-bad is
        whichever slot is not the candidate.

        Raises:
            OtaError: A slot-status read exited nonzero, `rauc status mark-bad` exited
                nonzero, or the board did not come back on the expected slot.
            Exception: ssh did not answer again within the wait bound after rebooting.
            OSError: The marker file exists but could not be removed.
        """
        if not self._marker.exists():
            return
        candidate = self._marker.read_text().strip()
        booted, other = self._slots()
        if booted == candidate:
            self._mark_bad_and_verify(candidate, other)
        self._marker.unlink()

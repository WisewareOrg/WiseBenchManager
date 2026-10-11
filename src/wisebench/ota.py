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
    never re-exposing it. The marker's content is the candidate slot's name: `write_marker`
    writes it before `install`, `clear_marker` removes it after `rollback`, and `recover`
    reads it to detect a run that died with it still present.
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

    def _booted_slot(self):
        """Read the currently booted slot's name from the board.

        Returns:
            str: The booted slot's name (e.g. `rootfs.0`), from the `slots` array of `rauc
            status --output-format=json` -- the identifier `rauc status mark-bad`/
            `mark-good` accept, not the top-level `booted` field's bootname (`A`/`B`).

        Raises:
            OtaError: `rauc status` exited nonzero.
        """
        stdout, stderr, returncode = self._driver.run("rauc status --output-format=json")
        if returncode != 0:
            raise OtaError(f"rauc status exited {returncode}: {stderr}")
        slots = json.loads(stdout[0])["slots"]
        return next(
            name for slot in slots for name, info in slot.items() if info["state"] == "booted"
        )

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

    def install(self, bundle, destination):
        """Put a bundle on the board, install it with rauc, and reboot into it.

        `reboot`'s own exit status is not checked: it is unreliable once the board goes down.

        Args:
            bundle (str | os.PathLike): Path to the local `.raucb` bundle file.
            destination (str): Path on the board to copy the bundle to before installing it.

        Returns:
            tuple[str, str]: The booted slot's name (e.g. `rootfs.0`) after the reboot, and
            the bundle file's sha256 hex digest.

        Raises:
            OSError: `bundle` could not be opened to compute its sha256.
            OtaError: `rauc install` or the slot-status read exited nonzero.
            Exception: ssh did not answer again within the wait bound after rebooting.
        """
        with open(bundle, "rb") as f:
            sha256 = hashlib.file_digest(f, "sha256").hexdigest()
        self._driver.put(str(bundle), destination)
        _, stderr, returncode = self._driver.run(f"rauc install {destination}")
        if returncode != 0:
            raise OtaError(f"rauc install exited {returncode}: {stderr}")
        self._driver.run("reboot")
        self._wait_for_ssh()
        return self._booted_slot(), sha256

    def rollback(self, slot):
        """Mark a slot bad and reboot off it.

        `reboot`'s own exit status is not checked: it is unreliable once the board goes down.

        Args:
            slot (str): The slot's name (e.g. `rootfs.0`) to mark bad.

        Returns:
            str: The booted slot's name after the reboot.

        Raises:
            OtaError: `rauc status mark-bad` or the slot-status read exited nonzero.
            Exception: ssh did not answer again within the wait bound after rebooting.
        """
        _, stderr, returncode = self._driver.run(f"rauc status mark-bad {slot}")
        if returncode != 0:
            raise OtaError(f"rauc status mark-bad exited {returncode}: {stderr}")
        self._driver.run("reboot")
        self._wait_for_ssh()
        return self._booted_slot()

    def recover(self):
        """Undo a run that died between `install` and `rollback`, then clear the marker.

        Raises:
            OtaError: The slot-status read, or `rollback`'s own mark-bad, exited nonzero.
            Exception: ssh did not answer again within the wait bound after rebooting.
            OSError: The marker file exists but could not be removed.
        """
        if not self._marker.exists():
            return
        candidate = self._marker.read_text().strip()
        if self._booted_slot() == candidate:
            self.rollback(candidate)
        self.clear_marker()

    def write_marker(self, slot):
        """Write `slot` to the marker file.

        Args:
            slot (str): The candidate slot's name.

        Raises:
            OSError: The marker file could not be written.
        """
        self._marker.write_text(slot)

    def clear_marker(self):
        """Remove the marker file, if present.

        Raises:
            OSError: The marker file exists but could not be removed.
        """
        self._marker.unlink(missing_ok=True)

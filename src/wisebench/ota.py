"""OTA install, rollback and mid-OTA recovery over labgrid's SSHDriver and rauc.

The mid-OTA marker is a file at a caller-chosen path. Its content is the candidate slot's
name. The caller constructs one `Ota` per run: `write_marker` writes the marker before
`install`, `clear_marker` removes it after `rollback`, and `recover` reads it to detect a run
that died with it still present.
"""

import hashlib
import json

from tenacity import Retrying, stop_after_delay, wait_fixed

_WAIT_TIMEOUT = 300
_WAIT_INTERVAL = 5


class OtaError(Exception):
    """A rauc command exited non-zero."""


class Ota:
    """Install, rollback and mid-OTA recovery for one board, over one labgrid SSHDriver.

    Holds the driver, the mid-OTA marker's path, and the ssh-wait bound. Calls the driver;
    never re-exposes it.
    """

    def __init__(self, driver, marker, timeout=_WAIT_TIMEOUT, interval=_WAIT_INTERVAL):
        """Bind one Ota to a board and a marker file.

        Args:
            driver: A labgrid SSHDriver (or stand-in) bound to a labgrid Target.
            marker (pathlib.Path): Path to the mid-OTA marker file.
            timeout (float): Seconds to keep retrying an ssh reconnect before giving up.
            interval (float): Seconds to wait between ssh reconnect retries.

        Returns:
            None.
        """
        self._driver = driver
        self._marker = marker
        self._timeout = timeout
        self._interval = interval

    def _run(self, cmd):
        """Run a command on the board and raise on a nonzero exit.

        Args:
            cmd (str): The command line to run on the board.

        Returns:
            list[str]: The command's stdout, one line per element.

        Raises:
            OtaError: The command's exit code was nonzero.
        """
        stdout, stderr, returncode = self._driver.run(cmd)
        if returncode != 0:
            raise OtaError(f"{cmd!r} exited {returncode}: {stderr}")
        return stdout

    def _booted_slot(self):
        """Read the currently booted slot's name from the board.

        Returns:
            str: The booted slot's name (e.g. `rootfs.0`), from the `slots` array of `rauc
            status --output-format=json` -- the identifier `rauc status mark-bad`/
            `mark-good` accept, not the top-level `booted` field's bootname (`A`/`B`).

        Raises:
            OtaError: `rauc status` exited nonzero.
        """
        (line,) = self._run("rauc status --output-format=json")
        slots = json.loads(line)["slots"]
        return next(
            name for slot in slots for name, info in slot.items() if info["state"] == "booted"
        )

    def _wait_for_ssh(self):
        """Reconnect and run a trivial command until ssh answers again.

        Reconnecting re-establishes the SSH control master labgrid tears down on reboot
        (`driver.target.deactivate`/`.activate`).

        Returns:
            None.

        Raises:
            Exception: The last exception raised by reconnecting or running, once the
                bound timeout elapses without ssh answering.
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

        Args:
            bundle (str | os.PathLike): Path to the local `.raucb` bundle file.
            destination (str): Path on the board to copy the bundle to before installing it.

        Returns:
            tuple[str, str]: The booted slot's name (e.g. `rootfs.0`) after the reboot, and
            the bundle file's sha256 hex digest.

        Raises:
            OtaError: `rauc install` or the slot-status read exited nonzero.
            Exception: ssh did not answer again within the wait bound after rebooting.
        """
        with open(bundle, "rb") as f:
            sha256 = hashlib.file_digest(f, "sha256").hexdigest()
        self._driver.put(str(bundle), destination)
        self._run(f"rauc install {destination}")
        self._driver.run("reboot")
        self._wait_for_ssh()
        return self._booted_slot(), sha256

    def rollback(self, slot):
        """Mark a slot bad and reboot off it.

        Args:
            slot (str): The slot's name (e.g. `rootfs.0`) to mark bad.

        Returns:
            str: The booted slot's name after the reboot.

        Raises:
            OtaError: `rauc status mark-bad` or the slot-status read exited nonzero.
            Exception: ssh did not answer again within the wait bound after rebooting.
        """
        self._run(f"rauc status mark-bad {slot}")
        self._driver.run("reboot")
        self._wait_for_ssh()
        return self._booted_slot()

    def recover(self):
        """Undo a run that died between `install` and `rollback`.

        If the marker exists and the board is still booted on the slot it names, mark that
        slot bad and reboot off it. Either way, remove the marker once the board is safe.

        Returns:
            None.

        Raises:
            OtaError: `rauc status mark-bad` or a slot-status read exited nonzero.
            Exception: ssh did not answer again within the wait bound after rebooting.
        """
        if not self._marker.exists():
            return
        candidate = self._marker.read_text().strip()
        if self._booted_slot() == candidate:
            self._run(f"rauc status mark-bad {candidate}")
            self._driver.run("reboot")
            self._wait_for_ssh()
        self.clear_marker()

    def write_marker(self, slot):
        """Write the mid-OTA marker file.

        Args:
            slot (str): The candidate slot's name, written as the marker's content.

        Returns:
            None.

        Raises:
            OSError: The marker file could not be written.
        """
        self._marker.write_text(slot)

    def clear_marker(self):
        """Remove the mid-OTA marker file, if present.

        Returns:
            None.

        Raises:
            OSError: The marker file exists but could not be removed.
        """
        self._marker.unlink(missing_ok=True)

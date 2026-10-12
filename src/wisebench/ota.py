"""OTA install over labgrid's SSHDriver and rauc."""

import hashlib
import json

from tenacity import Retrying, stop_after_delay, wait_fixed

_WAIT_TIMEOUT = 300
_WAIT_INTERVAL = 5


class OtaError(Exception):
    """A rauc command exited non-zero."""


class Ota:
    """Install a bundle on one board, over one labgrid SSHDriver.

    Holds the driver and the ssh-wait bound; calls the driver, never re-exposing it. `install`
    reads the candidate slot (the board's other slot) before installing, and raises if the
    board comes back on any slot but that one (a rauc fallback). rauc always installs to
    whichever slot is inactive, so which slot the board rests on before a run is not a
    concern.
    """

    def __init__(self, driver, timeout=_WAIT_TIMEOUT, interval=_WAIT_INTERVAL):
        """Bind one Ota to a board.

        Args:
            driver: A labgrid SSHDriver (or stand-in) bound to a labgrid Target.
            timeout (float): Seconds to keep retrying an ssh reconnect before giving up.
            interval (float): Seconds to wait between ssh reconnect retries.
        """
        self._driver = driver
        self._timeout = timeout
        self._interval = interval

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
        booted = next(n for slot in slots for n, info in slot.items() if info["state"] == "booted")
        other = next(n for slot in slots for n, info in slot.items() if info["state"] != "booted")
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

    def install(self, bundle, destination):
        """Put a bundle on the board, install it with rauc, and reboot into it.

        Reads the candidate slot (the board's other slot) before installing. `reboot`'s own
        exit status is not checked: it is unreliable once the board goes down. Verifies the
        board actually booted the candidate slot once it comes back, raising if a rauc
        fallback left it on the previous slot instead.

        Args:
            bundle (str | os.PathLike): Path to the local `.raucb` bundle file.
            destination (str): Path on the board to copy the bundle to before installing it.

        Returns:
            str: The bundle file's sha256 hex digest.

        Raises:
            OSError: `bundle` could not be opened to compute its sha256.
            OtaError: `rauc install` or a slot-status read exited nonzero, or the board came
                back on a slot other than the candidate (a rauc fallback).
            Exception: ssh did not answer again within the wait bound after rebooting.
        """
        _, candidate = self._slots()

        with open(bundle, "rb") as f:
            sha256 = hashlib.file_digest(f, "sha256").hexdigest()
        self._driver.put(str(bundle), destination)
        _, stderr, returncode = self._driver.run(f"rauc install {destination}")
        if returncode != 0:
            raise OtaError(f"rauc install exited {returncode}: {stderr}")
        self._driver.run("reboot")
        self._wait_for_ssh()
        booted, _ = self._slots()
        if booted != candidate:
            raise OtaError(f"expected {candidate} booted after install, found {booted}")
        return sha256

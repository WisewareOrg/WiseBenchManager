"""The slot read from `rauc status` JSON, and the ssh wait's bound."""

import hashlib
import json
import time

import pytest
from labgrid.driver import ExecutionError

from wisebench import ota


class FakeTarget:
    """Each `activate` call pops the next scripted result; an Exception instance is raised."""

    def __init__(self, activate_results=None):
        self.activate_calls = 0
        self._activate_results = list(activate_results) if activate_results else None

    def deactivate(self, driver):
        pass

    def activate(self, driver):
        self.activate_calls += 1
        if self._activate_results:
            result = self._activate_results.pop(0)
            if isinstance(result, Exception):
                raise result


class FakeDriver:
    """Each `run` call pops the next scripted result; an Exception instance is raised."""

    def __init__(self, run_results, activate_results=None):
        self.target = FakeTarget(activate_results)
        self._run_results = list(run_results)
        self.run_calls = []
        self.put_calls = []

    def run(self, cmd):
        self.run_calls.append(cmd)
        result = self._run_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def put(self, filename, remotepath):
        self.put_calls.append((filename, remotepath))


def _rauc_status(booted_slot, other_slot):
    slots = [
        {booted_slot: {"state": "booted"}},
        {other_slot: {"state": "inactive"}},
    ]
    return ([json.dumps({"compatible": "x", "slots": slots})], [], 0)


RAUC_STATUS_OK = _rauc_status("rootfs.1", "rootfs.0")  # booted: rootfs.1, other: rootfs.0


def test_default_wait_timeout_is_300_seconds():
    assert ota._WAIT_TIMEOUT == 300


def test_slots_reads_the_booted_and_other_names():
    bench = ota.Ota(FakeDriver([RAUC_STATUS_OK]))
    assert bench._slots() == ("rootfs.1", "rootfs.0")


def test_slots_raises_ota_error_on_nonzero_exit():
    bench = ota.Ota(FakeDriver([([], ["boom"], 1)]))
    with pytest.raises(ota.OtaError, match="boom"):
        bench._slots()


def test_install_returns_the_bundles_sha256(tmp_path):
    bundle = tmp_path / "bundle.raucb"
    bundle.write_bytes(b"x")
    driver = FakeDriver(
        [
            RAUC_STATUS_OK,  # candidate=rootfs.0
            ([], [], 0),  # rauc install
            ([], [], 0),  # reboot
            ([], [], 0),  # _wait_for_ssh's probe
            _rauc_status("rootfs.0", "rootfs.1"),  # booted slot after reboot
        ]
    )
    bench = ota.Ota(driver)
    sha256 = bench.install(bundle, "/data/update.raucb")
    assert sha256 == hashlib.sha256(b"x").hexdigest()


def test_install_raises_ota_error_when_the_board_falls_back_to_the_previous_slot(tmp_path):
    bundle = tmp_path / "bundle.raucb"
    bundle.write_bytes(b"x")
    driver = FakeDriver(
        [
            RAUC_STATUS_OK,  # candidate=rootfs.0
            ([], [], 0),  # rauc install
            ([], [], 0),  # reboot
            ([], [], 0),  # _wait_for_ssh's probe
            RAUC_STATUS_OK,  # fallback: still booted on rootfs.1, never reached the candidate
        ]
    )
    bench = ota.Ota(driver)
    with pytest.raises(ota.OtaError, match="expected rootfs.0 booted"):
        bench.install(bundle, "/data/update.raucb")


def test_install_raises_ota_error_on_nonzero_rauc_install(tmp_path):
    bundle = tmp_path / "bundle.raucb"
    bundle.write_bytes(b"x")
    driver = FakeDriver([RAUC_STATUS_OK, ([], ["boom"], 1)])
    bench = ota.Ota(driver)
    with pytest.raises(ota.OtaError, match="boom"):
        bench.install(bundle, "/data/update.raucb")


def test_wait_for_ssh_reconnects_and_returns_once_run_succeeds():
    driver = FakeDriver([ExecutionError("down"), ExecutionError("down"), ([], [], 0)])
    bench = ota.Ota(driver, timeout=1, interval=0.01)
    bench._wait_for_ssh()
    assert driver.target.activate_calls == 3


def test_wait_for_ssh_retries_past_a_bare_connect_timeout():
    # labgrid's own SSHDriver raises a bare Exception, not ExecutionError, when its internal
    # connect timeout expires.
    driver = FakeDriver(
        [([], [], 0)],
        activate_results=[Exception("Timeout while waiting for ssh connection"), None],
    )
    bench = ota.Ota(driver, timeout=1, interval=0.01)
    bench._wait_for_ssh()
    assert driver.target.activate_calls == 2


def test_wait_for_ssh_times_out_when_ssh_never_comes_back():
    driver = FakeDriver([ExecutionError("down")] * 1000)
    bench = ota.Ota(driver, timeout=0.2, interval=0.05)
    start = time.monotonic()
    with pytest.raises(ExecutionError):
        bench._wait_for_ssh()
    assert time.monotonic() - start < 2

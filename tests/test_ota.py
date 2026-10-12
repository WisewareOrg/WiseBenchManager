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


def test_previous_slot_and_candidate_slot_are_none_before_install():
    bench = ota.Ota(FakeDriver([]))
    assert bench.previous_slot is None
    assert bench.candidate_slot is None


def test_previous_slot_and_candidate_slot_read_back_what_install_recorded(tmp_path):
    bundle = tmp_path / "bundle.raucb"
    bundle.write_bytes(b"x")
    driver = FakeDriver(
        [
            RAUC_STATUS_OK,  # previous=rootfs.1, candidate=rootfs.0
            ([], [], 0),  # rauc install
            ([], [], 0),  # reboot
            ([], [], 0),  # _wait_for_ssh's probe
            _rauc_status("rootfs.0", "rootfs.1"),  # booted slot after reboot
        ]
    )
    bench = ota.Ota(driver)
    bench.install(bundle, "/data/update.raucb")
    assert bench.previous_slot == "rootfs.1"
    assert bench.candidate_slot == "rootfs.0"


def test_install_records_previous_and_candidate_slots(tmp_path):
    bundle = tmp_path / "bundle.raucb"
    bundle.write_bytes(b"x")
    driver = FakeDriver(
        [
            RAUC_STATUS_OK,  # previous=rootfs.1, candidate=rootfs.0
            ([], [], 0),  # rauc install
            ([], [], 0),  # reboot
            ([], [], 0),  # _wait_for_ssh's probe
            _rauc_status("rootfs.0", "rootfs.1"),  # booted slot after reboot
        ]
    )
    bench = ota.Ota(driver)
    slot, sha256 = bench.install(bundle, "/data/update.raucb")
    assert bench._previous_slot == "rootfs.1"
    assert bench._candidate_slot == "rootfs.0"
    assert slot == "rootfs.0"
    assert sha256 == hashlib.sha256(b"x").hexdigest()


def test_install_raises_ota_error_when_the_board_falls_back_to_the_previous_slot(tmp_path):
    bundle = tmp_path / "bundle.raucb"
    bundle.write_bytes(b"x")
    driver = FakeDriver(
        [
            RAUC_STATUS_OK,  # previous=rootfs.1, candidate=rootfs.0
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


def test_rollback_marks_the_candidate_bad_and_reboots_onto_the_previous_slot():
    driver = FakeDriver(
        [
            ([], [], 0),  # rauc status mark-bad rootfs.0
            ([], [], 0),  # reboot
            ([], [], 0),  # _wait_for_ssh's probe
            RAUC_STATUS_OK,  # booted slot after reboot: rootfs.1
        ]
    )
    bench = ota.Ota(driver)
    bench._previous_slot = "rootfs.1"
    bench._candidate_slot = "rootfs.0"
    slot = bench.rollback()
    assert slot == "rootfs.1"
    assert "rauc status mark-bad rootfs.0" in driver.run_calls


def test_rollback_raises_runtime_error_when_install_has_not_run():
    driver = FakeDriver([])
    bench = ota.Ota(driver)
    with pytest.raises(RuntimeError, match="install"):
        bench.rollback()
    assert driver.run_calls == []


def test_rollback_raises_ota_error_on_nonzero_mark_bad():
    bench = ota.Ota(FakeDriver([([], ["boom"], 1)]))
    bench._candidate_slot = "rootfs.0"
    with pytest.raises(ota.OtaError, match="boom"):
        bench.rollback()


def test_rollback_raises_ota_error_when_the_previous_slot_is_not_booted():
    driver = FakeDriver(
        [
            ([], [], 0),  # rauc status mark-bad rootfs.0
            ([], [], 0),  # reboot
            ([], [], 0),  # _wait_for_ssh's probe
            _rauc_status("rootfs.0", "rootfs.1"),  # still on the candidate, not the previous
        ]
    )
    bench = ota.Ota(driver)
    bench._previous_slot = "rootfs.1"
    bench._candidate_slot = "rootfs.0"
    with pytest.raises(ota.OtaError, match="expected rootfs.1 booted"):
        bench.rollback()


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

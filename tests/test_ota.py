"""The slot read from `rauc status` JSON, the mid-OTA marker, and the ssh wait's bound."""

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


RAUC_STATUS_OK = (['{"compatible": "x", "booted": "B", "slots": []}'], [], 0)


def test_booted_slot_reads_the_json_field():
    driver = FakeDriver([RAUC_STATUS_OK])
    assert ota._booted_slot(driver) == "B"


def test_run_raises_ota_error_on_nonzero_exit():
    driver = FakeDriver([([], ["boom"], 1)])
    with pytest.raises(ota.OtaError, match="boom"):
        ota._run(driver, "rauc status mark-bad B")


def test_write_marker_then_clear_marker_round_trips(tmp_path):
    marker = tmp_path / "mid-ota"
    ota.write_marker(marker, "B")
    assert marker.read_text() == "B"
    ota.clear_marker(marker)
    assert not marker.exists()


def test_clear_marker_is_a_noop_when_absent(tmp_path):
    ota.clear_marker(tmp_path / "mid-ota")


def test_recover_does_nothing_without_a_marker(tmp_path):
    driver = FakeDriver([])
    ota.recover(driver, tmp_path / "mid-ota")
    assert driver.run_calls == []


def test_recover_marks_the_candidate_bad_when_it_is_still_booted(tmp_path):
    marker = tmp_path / "mid-ota"
    ota.write_marker(marker, "B")
    driver = FakeDriver(
        [
            RAUC_STATUS_OK,  # _booted_slot: still on the candidate "B"
            ([], [], 0),  # rauc status mark-bad B
            ([], [], 0),  # reboot
            ([], [], 0),  # _wait_for_ssh's probe
        ]
    )
    ota.recover(driver, marker)
    assert "rauc status mark-bad B" in driver.run_calls
    assert not marker.exists()


def test_recover_only_clears_the_marker_when_the_good_slot_is_already_booted(tmp_path):
    marker = tmp_path / "mid-ota"
    ota.write_marker(marker, "B")
    driver = FakeDriver([(['{"compatible": "x", "booted": "A", "slots": []}'], [], 0)])
    ota.recover(driver, marker)
    assert driver.run_calls == ["rauc status --output-format=json"]
    assert not marker.exists()


def test_wait_for_ssh_reconnects_and_returns_once_run_succeeds():
    driver = FakeDriver([ExecutionError("down"), ExecutionError("down"), ([], [], 0)])
    ota._wait_for_ssh(driver, timeout=1, interval=0.01)
    assert driver.target.activate_calls == 3


def test_wait_for_ssh_retries_past_a_bare_connect_timeout():
    # labgrid's own SSHDriver raises a bare Exception, not ExecutionError, when its
    # internal connect timeout expires (confirmed against labgrid 26.0 on the bench).
    driver = FakeDriver(
        [([], [], 0)],
        activate_results=[Exception("Timeout while waiting for ssh connection"), None],
    )
    ota._wait_for_ssh(driver, timeout=1, interval=0.01)
    assert driver.target.activate_calls == 2


def test_wait_for_ssh_times_out_when_ssh_never_comes_back():
    driver = FakeDriver([ExecutionError("down")] * 1000)
    start = time.monotonic()
    with pytest.raises(ExecutionError):
        ota._wait_for_ssh(driver, timeout=0.2, interval=0.05)
    assert time.monotonic() - start < 2

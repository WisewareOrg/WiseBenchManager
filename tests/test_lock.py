"""`Lock`: the exclusive, non-blocking lock `bench test` and `bench run` share."""

import fcntl

import pytest

from wisebench import bench_test, lock


def test_lock_creates_the_directory_and_the_lock_file(tmp_path):
    directory = tmp_path / "records"
    with lock.Lock(directory):
        assert (directory / "lock").exists()


def test_lock_releases_so_a_second_acquisition_succeeds_afterwards(tmp_path):
    directory = tmp_path / "records"
    with lock.Lock(directory):
        pass
    with lock.Lock(directory):
        pass


def test_lock_raises_bench_failure_when_another_process_holds_it(tmp_path):
    directory = tmp_path / "records"
    directory.mkdir()
    with open(directory / "lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with (
                pytest.raises(bench_test.BenchFailure, match="another run holds the lock"),
                lock.Lock(directory),
            ):
                pass
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)

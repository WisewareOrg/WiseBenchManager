"""`Config.load`: reads `bench.toml` into the frozen dataclass."""

import pytest

from wisebench import config


def test_config_load_reads_hooks_bundle_and_suites(tmp_path):
    (tmp_path / "bench.toml").write_text(
        """
        [hooks]
        precondition = "true"
        build = "just build"
        bundle = "just bundle"

        [bundle]
        path = "build/update.raucb"
        destination = "/data/update.raucb"

        [tests]
        host_suite = "tests/host_suite"
        target_suite = "tests/target_suite"
        """
    )
    loaded = config.Config.load(tmp_path / "bench.toml")
    assert loaded.hooks == {"precondition": "true", "build": "just build", "bundle": "just bundle"}
    assert loaded.bundle_path == "build/update.raucb"
    assert loaded.bundle_destination == "/data/update.raucb"
    assert loaded.host_suite == "tests/host_suite"
    assert loaded.target_suite == "tests/target_suite"


def test_config_load_raises_keyerror_on_a_missing_table(tmp_path):
    (tmp_path / "bench.toml").write_text('[hooks]\nprecondition = "true"\n')
    with pytest.raises(KeyError):
        config.Config.load(tmp_path / "bench.toml")


def test_config_load_raises_keyerror_on_a_missing_destination(tmp_path):
    (tmp_path / "bench.toml").write_text(
        """
        [hooks]
        precondition = "true"
        build = "just build"
        bundle = "just bundle"

        [bundle]
        path = "build/update.raucb"

        [tests]
        host_suite = "tests/host_suite"
        target_suite = "tests/target_suite"
        """
    )
    with pytest.raises(KeyError, match="destination"):
        config.Config.load(tmp_path / "bench.toml")

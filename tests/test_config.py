"""`Config.load`: reads `bench.toml` into the frozen dataclass."""

import pytest

from wisebench import config


def test_config_load_reads_hooks_bundle_suites_and_github(tmp_path):
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

        [github]
        check_name = "bench"
        """
    )
    loaded = config.Config.load(tmp_path / "bench.toml")
    assert loaded.hooks == {"precondition": "true", "build": "just build", "bundle": "just bundle"}
    assert loaded.bundle_path == "build/update.raucb"
    assert loaded.bundle_destination == "/data/update.raucb"
    assert loaded.host_suite == "tests/host_suite"
    assert loaded.target_suite == "tests/target_suite"
    assert loaded.github == {"check_name": "bench"}


def test_config_load_raises_keyerror_on_a_missing_table(tmp_path):
    (tmp_path / "bench.toml").write_text('[hooks]\nprecondition = "true"\n')
    with pytest.raises(KeyError):
        config.Config.load(tmp_path / "bench.toml")

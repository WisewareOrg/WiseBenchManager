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

        [github]
        repo = "acme/layer"
        app_id = 1
        private_key = "/outside/the/tree/app.pem"
        check = "bench"
        """
    )
    loaded = config.Config.load(tmp_path / "bench.toml")
    assert loaded.hooks == {"precondition": "true", "build": "just build", "bundle": "just bundle"}
    assert loaded.bundle_path == "build/update.raucb"
    assert loaded.bundle_destination == "/data/update.raucb"
    assert loaded.host_suite == "tests/host_suite"
    assert loaded.target_suite == "tests/target_suite"
    assert loaded.github_repo == "acme/layer"
    assert loaded.github_app_id == 1
    assert loaded.github_private_key == "/outside/the/tree/app.pem"
    assert loaded.github_check == "bench"


def test_config_load_leaves_github_fields_none_when_the_table_is_absent(tmp_path):
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
    assert loaded.github_repo is None
    assert loaded.github_app_id is None
    assert loaded.github_private_key is None
    assert loaded.github_check is None


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

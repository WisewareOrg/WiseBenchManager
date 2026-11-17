"""`Config.load`: reads `bench.toml` into the frozen dataclass."""

import pytest
from helpers import settings_file

from wisebench import config


def test_config_load_reads_the_hooks_and_suites_from_the_layer_and_github_from_the_settings(
    tmp_path,
):
    """Config.load reads the hooks and suites from the layer's file, [github] from the settings."""
    (tmp_path / "bench.toml").write_text(
        """
        [hooks]
        precondition = "true"
        build = "just build"
        bundle = "just bundle"

        [tests]
        host_suite = "tests/host_suite"
        target_suite = "tests/target_suite"
        """
    )
    settings_file().parent.mkdir(parents=True, exist_ok=True)
    settings_file().write_text(
        """
        [github]
        repo = "acme/layer"
        app_id = 1
        private_key = "/outside/the/tree/app.pem"
        check = "bench"
        """
    )
    loaded = config.Config.load(tmp_path / "bench.toml")
    assert loaded.hooks == {"precondition": "true", "build": "just build", "bundle": "just bundle"}
    assert loaded.host_suite == "tests/host_suite"
    assert loaded.target_suite == "tests/target_suite"
    assert loaded.github_repo == "acme/layer"
    assert loaded.github_app_id == 1
    assert loaded.github_private_key == "/outside/the/tree/app.pem"
    assert loaded.github_check == "bench"
    assert loaded.run_unit is None


def test_config_load_reads_the_run_unit_from_the_settings_and_ignores_the_layers_tables(tmp_path):
    """A `[github]` or `[run]` in the layer's `bench.toml` is ignored; the settings' are used."""
    (tmp_path / "bench.toml").write_text(
        """
        [hooks]
        precondition = "true"
        build = "just build"
        bundle = "just bundle"

        [tests]
        host_suite = "tests/host_suite"
        target_suite = "tests/target_suite"

        [github]
        repo = "layer/ignored"
        app_id = 99
        private_key = "/layer/ignored.pem"
        check = "ignored"

        [run]
        unit = "layer-unit"
        """
    )
    settings_file().parent.mkdir(parents=True, exist_ok=True)
    settings_file().write_text(
        """
        [github]
        repo = "acme/layer"
        app_id = 1
        private_key = "/outside/the/tree/app.pem"
        check = "bench"

        [run]
        unit = "bench-run"
        """
    )
    loaded = config.Config.load(tmp_path / "bench.toml")
    assert loaded.run_unit == "bench-run"
    assert loaded.github_repo == "acme/layer"


def test_config_load_leaves_github_fields_none_when_the_settings_have_none(tmp_path):
    """Config.load leaves every github field None when the settings file has no [github]."""
    (tmp_path / "bench.toml").write_text(
        """
        [hooks]
        precondition = "true"
        build = "just build"
        bundle = "just bundle"

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
    """Config.load raises KeyError when a required table is missing."""
    (tmp_path / "bench.toml").write_text('[hooks]\nprecondition = "true"\n')
    with pytest.raises(KeyError):
        config.Config.load(tmp_path / "bench.toml")


def test_config_load_raises_keyerror_on_a_missing_target_suite(tmp_path):
    """Config.load raises KeyError when [tests] target_suite is missing."""
    (tmp_path / "bench.toml").write_text(
        """
        [hooks]
        precondition = "true"
        build = "just build"
        bundle = "just bundle"

        [tests]
        host_suite = "tests/host_suite"
        """
    )
    with pytest.raises(KeyError, match="target_suite"):
        config.Config.load(tmp_path / "bench.toml")

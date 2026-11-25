"""Config: the layer's `bench.toml` and the bench's own, parsed once into a frozen dataclass."""

import tomllib
from dataclasses import dataclass
from pathlib import Path


def settings_path():
    """The bench's own settings file, `~/.config/wisebench/bench.toml`.

    Returns:
        pathlib.Path: Where `[github]` and `[run]` are read from; the file may not exist.
    """
    return Path.home() / ".config" / "wisebench" / "bench.toml"


@dataclass(frozen=True)
class Config:
    """The bench's configuration, read once: the layer's hooks and suites, the bench's own settings.

    Args:
        hooks (dict[str, str]): Command line for "precondition", "build" and "bundle".
        host_suite (str): Path to the host-side pytest suite, relative to the checkout.
        target_suite (str): Path to the target-side pytest suite, relative to the checkout.
        github_repo (str | None): The repository the verdict is posted against, as
            "owner/name"; None without a `[github]` table in the settings file, which `bench run`
            requires.
        github_app_id (int | None): The GitHub App's id; None under the same condition.
        github_private_key (str | None): Path to the App's PEM-encoded private key, outside
            the checkout; None under the same condition.
        github_check (str | None): The check run's name, as the layer's ruleset lists it; None
            under the same condition.
        run_unit (str | None): The systemd unit base name (`[run] unit`) `bench run` stops
            on the bench's own failure; None without a `[run]` table in the settings file, which
            `bench run` requires.
    """

    hooks: dict
    host_suite: str
    target_suite: str
    github_repo: str | None = None
    github_app_id: int | None = None
    github_private_key: str | None = None
    github_check: str | None = None
    run_unit: str | None = None

    @classmethod
    def load(cls, path, settings):
        """Read and parse the layer's `bench.toml` and the bench's own settings file.

        `[hooks]` and `[tests]` come from the layer's file; `[github]` and `[run]` come only from
        the settings file, and a missing settings file leaves them None.

        Args:
            path (pathlib.Path): Path to `bench.toml` in the checkout.
            settings (pathlib.Path): The bench's own settings file; see `settings_path()`.

        Returns:
            Config: The parsed configuration.

        Raises:
            OSError: `path`, or an existing settings file, could not be read.
            tomllib.TOMLDecodeError: `path` or the settings file is not valid TOML.
            KeyError: A required table or key is missing.
        """
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        try:
            with open(settings, "rb") as f:
                bench = tomllib.load(f)
        except FileNotFoundError:
            bench = {}
        github = bench.get("github", {})
        return cls(
            hooks=raw["hooks"],
            host_suite=raw["tests"]["host_suite"],
            target_suite=raw["tests"]["target_suite"],
            github_repo=github.get("repo"),
            github_app_id=github.get("app_id"),
            github_private_key=github.get("private_key"),
            github_check=github.get("check"),
            run_unit=bench.get("run", {}).get("unit"),
        )

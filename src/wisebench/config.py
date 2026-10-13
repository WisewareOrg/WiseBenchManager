"""Config: `bench.toml`, parsed once into a frozen dataclass."""

import tomllib
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    """`bench.toml`, read once: the three hooks, the two suites.

    Args:
        hooks (dict[str, str]): Command line for "precondition", "build" and "bundle".
        host_suite (str): Path to the host-side pytest suite, relative to the checkout.
        target_suite (str): Path to the target-side pytest suite, relative to the checkout.
        github_repo (str | None): The repository the verdict is posted against, as
            "owner/name"; None when `bench.toml` has no `[github]` table, which is fine for
            `bench test` and a failure only `Check`, built by `bench run`, hits.
        github_app_id (int | None): The GitHub App's id; None under the same condition.
        github_private_key (str | None): Path to the App's PEM-encoded private key, outside
            the checkout; None under the same condition.
        github_check (str | None): The check run's name, as the layer's ruleset lists it; None
            under the same condition.
        run_unit (str | None): The systemd unit base name (`[run] unit`) `bench run` disables
            on the bench's own failure; None when `bench.toml` has no `[run]` table, which is
            fine for `bench test` and a failure only `bench run` itself hits.
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
    def load(cls, path):
        """Read and parse `bench.toml`.

        Keys only, no validation beyond the `KeyError` a missing one raises naturally.

        Args:
            path (pathlib.Path): Path to `bench.toml` in the checkout.

        Returns:
            Config: The parsed configuration.

        Raises:
            OSError: `path` could not be read.
            tomllib.TOMLDecodeError: `path` is not valid TOML.
            KeyError: A required table or key is missing.
        """
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        github = raw.get("github", {})
        return cls(
            hooks=raw["hooks"],
            host_suite=raw["tests"]["host_suite"],
            target_suite=raw["tests"]["target_suite"],
            github_repo=github.get("repo"),
            github_app_id=github.get("app_id"),
            github_private_key=github.get("private_key"),
            github_check=github.get("check"),
            run_unit=raw.get("run", {}).get("unit"),
        )

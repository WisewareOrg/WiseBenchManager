"""Config: `bench.toml`, parsed once into a frozen dataclass."""

import tomllib
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    """`bench.toml`, read once: the three hooks, the bundle, the two suites.

    Args:
        hooks (dict[str, str]): Command line for "precondition", "build" and "bundle".
        bundle_path (str): Path to the built bundle, relative to the checkout.
        bundle_destination (str): Path on the board to stage the bundle at before `rauc
            install`; the layer's own fact, never this runner's.
        host_suite (str): Path to the host-side pytest suite, relative to the checkout.
        target_suite (str): Path to the target-side pytest suite, relative to the checkout.
    """

    hooks: dict
    bundle_path: str
    bundle_destination: str
    host_suite: str
    target_suite: str

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
        return cls(
            hooks=raw["hooks"],
            bundle_path=raw["bundle"]["path"],
            bundle_destination=raw["bundle"]["destination"],
            host_suite=raw["tests"]["host_suite"],
            target_suite=raw["tests"]["target_suite"],
        )

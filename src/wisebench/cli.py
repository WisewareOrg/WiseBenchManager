"""`bench test <ref>`: checkout, hooks, both suites, OTA, and a terminal verdict."""

import argparse
import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from labgrid import Environment

from wisebench.ota import Ota

_TIMESTAMP = "%Y%m%dT%H%M%SZ"
_HOOK_NAMES = ("precondition", "build", "bundle")


@dataclass(frozen=True)
class Config:
    """`bench.toml`, read once: the three hooks, the bundle, the two suites, github.

    Args:
        hooks (dict[str, str]): Command line for each of "precondition", "build" and "bundle".
        bundle_path (str): Path to the built bundle, relative to the checkout.
        bundle_destination (str): Path on the board to stage the bundle at before `rauc
            install`; the layer's own fact, never this runner's.
        host_suite (str): Path to the host-side pytest suite, relative to the checkout.
        target_suite (str): Path to the target-side pytest suite, relative to the checkout.
        github (dict): The `[github]` table, unread here and carried for a later command.
    """

    hooks: dict
    bundle_path: str
    bundle_destination: str
    host_suite: str
    target_suite: str
    github: dict

    @classmethod
    def load(cls, path):
        """Read and parse `bench.toml`.

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
            github=raw.get("github", {}),
        )


class BenchTest:
    """One `bench test <ref>` run: the checkout, the config, the result directory and the `Ota`.

    Holds what this run's steps share -- the checkout root, the resolved ref and commit, the
    config `load_config` reads, the result directory and its own timestamp, and the `Ota`
    instance `recover` builds and `install`/`rollback` reuse -- so each step method reads and
    writes only this state instead of re-deriving it. Call order: `resolve_and_checkout`,
    `load_config`, `recover`, `run_hook` for each of "precondition"/"build"/"bundle",
    `run_pytest` on the host suite, `install`, `run_pytest` on the target suite, `rollback`;
    `record` may follow either `run_pytest` call once that call's own `run.json` exists.
    """

    def __init__(self, ref, set_name, pytest_args, lg_env, result_base):
        """Bind one run to a ref, a replay set name, extra pytest arguments and an env file.

        Args:
            ref (str): The git ref to resolve, check out and test.
            set_name (str | None): The replay set name from `--set`, or None.
            pytest_args (list[str]): Arguments after `--`, passed to pytest unchanged.
            lg_env (str): Path to the labgrid env file (`LG_ENV`).
            result_base (pathlib.Path): `$BENCH_RESULT_DIR`, shared with every subprocess
                and holding the mid-OTA marker; stable across runs.
        """
        self.checkout = Path.cwd()
        self.ref = ref
        self.set_name = set_name or ""
        self.pytest_args = list(pytest_args)
        self.lg_env = lg_env
        self.result_base = result_base
        self.stamp = datetime.now(UTC).strftime(_TIMESTAMP)
        self.commit = None
        self.config = None
        self._ota = None

    def resolve_and_checkout(self):
        """Resolve `self.ref` to a commit, refuse a dirty tree, and check out that commit.

        Returns:
            str: The resolved commit hex, also stored on `self.commit`.

        Raises:
            RuntimeError: The tree has modified or staged tracked files.
            subprocess.CalledProcessError: The ref did not resolve, or the checkout failed.
        """
        rev_parse = subprocess.run(
            ["git", "rev-parse", "--verify", f"{self.ref}^{{commit}}"],
            cwd=self.checkout,
            capture_output=True,
            text=True,
            check=True,
        )
        self.commit = rev_parse.stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.checkout,
            capture_output=True,
            text=True,
            check=True,
        )
        dirty = [line for line in status.stdout.splitlines() if not line.startswith("??")]
        if dirty:
            raise RuntimeError(f"tree is dirty: {dirty}")
        subprocess.run(
            ["git", "checkout", self.commit],
            cwd=self.checkout,
            capture_output=True,
            text=True,
            check=True,
        )
        return self.commit

    def load_config(self):
        """Read `bench.toml` from the checkout root.

        Returns:
            Config: The parsed configuration, also stored on `self.config`.

        Raises:
            OSError: `bench.toml` could not be read.
            tomllib.TOMLDecodeError: `bench.toml` is not valid TOML.
            KeyError: A required table or key is missing.
        """
        self.config = Config.load(self.checkout / "bench.toml")
        return self.config

    def _subprocess_env(self):
        """Build the environment every hook and pytest subprocess runs with.

        Returns:
            dict[str, str]: This process's environment plus BENCH_SHA, BENCH_TREE,
            BENCH_RESULT_DIR and BENCH_SET.
        """
        return {
            **os.environ,
            "BENCH_SHA": self.commit,
            "BENCH_TREE": str(self.checkout),
            "BENCH_RESULT_DIR": str(self.result_base),
            "BENCH_SET": self.set_name,
        }

    def run_hook(self, name):
        """Run one configured hook as a subprocess, streaming its output.

        Output is written to the terminal and to `<result_base>/<stamp>/<name>.log`. Called
        once for each of "precondition", "build" and "bundle".

        Args:
            name (str): One of "precondition", "build" or "bundle".

        Returns:
            int: The hook's exit status.

        Raises:
            OSError: The log file could not be opened.
        """
        log_dir = self.result_base / self.stamp
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / f"{name}.log", "w", encoding="utf-8") as log:
            process = subprocess.Popen(
                self.config.hooks[name],
                shell=True,
                cwd=self.checkout,
                env=self._subprocess_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for line in process.stdout:
                print(line, end="")
                log.write(line)
            return process.wait()

    def recover(self):
        """Build this run's `Ota` and undo a mid-OTA crash a prior run left behind.

        Builds the labgrid `Target`/`SSHDriver` from `self.lg_env` through labgrid's own
        `Environment` API (`Environment(config_file=...).get_target("main").get_driver(
        "SSHDriver")`), and constructs the one `Ota` instance `install` and `rollback` reuse.

        Raises:
            Exception: labgrid could not build the target or the driver.
            OtaError: A slot-status read or mark-bad exited nonzero, or the board did not
                come back on the expected slot.
        """
        target = Environment(config_file=self.lg_env).get_target("main")
        driver = target.get_driver("SSHDriver")
        self._ota = Ota(driver, self.result_base / "mid-ota")
        self._ota.recover()

    def run_pytest(self, suite, *, lg_env=False):
        """Run pytest on one suite directory and locate the `run.json` it wrote.

        Args:
            suite (str): The suite's path, relative to the checkout.
            lg_env (bool): Whether to pass `--lg-env` and `--report-log` (the target suite).

        Returns:
            tuple[int, pathlib.Path | None]: pytest's exit status, and the directory its
            `run.json` landed in (None if exactly one new directory did not appear).
        """
        before = set(self.result_base.iterdir()) if self.result_base.exists() else set()
        # This process's own interpreter, not `uv run`: the checkout's cwd may hold its own
        # pyproject.toml, and pytest must run where wisebench's plugin and labgrid are installed.
        command = [sys.executable, "-m", "pytest", suite]
        if lg_env:
            command += [
                "--lg-env",
                self.lg_env,
                "--report-log",
                str(self.result_base / self.stamp / "report.jsonl"),
            ]
        command += self.pytest_args
        result = subprocess.run(command, cwd=self.checkout, env=self._subprocess_env())
        after = set(self.result_base.iterdir())
        new_dirs = [p for p in after - before if p.is_dir()]
        return result.returncode, new_dirs[0] if len(new_dirs) == 1 else None

    def install(self):
        """Install the configured bundle and boot into it.

        Returns:
            tuple[str, str]: The booted slot and the bundle's sha256.

        Raises:
            OSError: The bundle could not be opened.
            OtaError: `rauc install` or a slot-status read exited nonzero.
            Exception: ssh did not answer again within the wait bound.
        """
        bundle = self.checkout / self.config.bundle_path
        return self._ota.install(bundle, self.config.bundle_destination)

    def rollback(self):
        """Roll back to the previous slot.

        Returns:
            str: The booted slot after the rollback.

        Raises:
            RuntimeError: `install` has not run.
            OtaError: `rauc status mark-bad` or a slot-status read exited nonzero, or the
                board did not come back on the previous slot.
            Exception: ssh did not answer again within the wait bound.
        """
        return self._ota.rollback()

    def record(self, run_dir, **fields):
        """Merge fields into one pytest invocation's `run.json`.

        Called after the host suite (ref, commit, set), and after the target suite (those
        plus the bundle's sha256 and the slots).

        Args:
            run_dir (pathlib.Path): The directory `run.json` was written to.
            **fields: Fields to merge in.

        Raises:
            OSError: `run.json` could not be read or written.
            json.JSONDecodeError: `run.json` is not valid JSON.
        """
        path = run_dir / "run.json"
        data = json.loads(path.read_text())
        data.update(fields)
        path.write_text(json.dumps(data) + "\n")


def main(argv=None):
    """Parse arguments and run one `bench test` invocation end to end.

    Args:
        argv (list[str] | None): Arguments to parse, defaulting to `sys.argv[1:]`.

    Returns:
        int: 0 pass, 1 a hook or a test failed (the candidate's), 2 a step of ours raised
        (the bench's).
    """
    argv = sys.argv[1:] if argv is None else argv
    if "--" in argv:
        split = argv.index("--")
        own_args, pytest_args = argv[:split], argv[split + 1 :]
    else:
        own_args, pytest_args = argv, []

    parser = argparse.ArgumentParser(prog="bench")
    subparsers = parser.add_subparsers(dest="command", required=True)
    test_parser = subparsers.add_parser("test")
    test_parser.add_argument("ref")
    test_parser.add_argument("--set", dest="set_name", default=None)
    args = parser.parse_args(own_args)

    lg_env = os.environ.get("LG_ENV")
    if not lg_env:
        print("BENCH ERROR: LG_ENV is not set", file=sys.stderr)
        return 2

    result_base = Path(os.environ.get("BENCH_RESULT_DIR", "records"))
    result_base.mkdir(parents=True, exist_ok=True)
    bench = BenchTest(args.ref, args.set_name, pytest_args, lg_env, result_base)

    try:
        bench.resolve_and_checkout()
        bench.load_config()
        bench.recover()

        for hook in _HOOK_NAMES:
            if bench.run_hook(hook) != 0:
                print(f"FAIL: {hook}")
                return 1

        host_rc, host_run_dir = bench.run_pytest(bench.config.host_suite)
        if host_run_dir is not None:
            bench.record(host_run_dir, ref=bench.ref, commit=bench.commit, set=bench.set_name)
        if host_rc != 0:
            print("FAIL: host_suite")
            return 1

        slot_after_install, bundle_sha256 = bench.install()
        target_rc, target_run_dir = bench.run_pytest(bench.config.target_suite, lg_env=True)
        slot_after_rollback = bench.rollback()

        if target_run_dir is not None:
            bench.record(
                target_run_dir,
                ref=bench.ref,
                commit=bench.commit,
                set=bench.set_name,
                bundle_sha256=bundle_sha256,
                slots={"after_install": slot_after_install, "after_rollback": slot_after_rollback},
            )
        if target_rc != 0:
            print("FAIL: target_suite")
            return 1

        print("PASS")
        return 0
    except Exception as exc:
        print(f"BENCH ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

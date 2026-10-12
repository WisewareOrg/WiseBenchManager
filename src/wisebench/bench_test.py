"""BenchTest: one `bench test <ref>` run — checkout, hooks, both suites, OTA, the record."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from labgrid import Environment

from wisebench.config import Config
from wisebench.ota import Ota
from wisebench.plugin import TIMESTAMP_FORMAT


class BenchTest:
    """One `bench test <ref>` run: the checkout, the config, the result directory and the `Ota`.

    Holds what this run's steps share -- the checkout root, the resolved ref and commit, the
    config `load_config` reads, the result directory and its own timestamp, the `Ota` instance
    `recover` builds, and the bundle's sha256 and booted slots `install`/`run_target_suite`
    record for `rollback` to finish. Call order: `resolve_and_checkout`, `load_config`,
    `recover`, `run_hook` for each of `wisebench.config.HOOK_NAMES`, `run_host_suite`,
    `install`, `run_target_suite`, `rollback`.

    `recover` deactivates the labgrid driver once the mid-OTA check is done; `install`
    reactivates it. A hook can run for hours, and labgrid's `SSHDriver` tears its own ssh
    master and keepalive down once the board is unreachable for about 45 seconds, so holding
    the driver active across the hooks risks `install` inheriting a dead connection.
    """

    def __init__(self, ref, set_name, pytest_args, lg_env, result_base):
        """Bind one run to a ref, a replay set name, extra pytest arguments and an env file.

        Args:
            ref (str): The git ref to resolve, check out and test.
            set_name (str | None): The replay set name from `--set`, or None.
            pytest_args (list[str]): Arguments after `--`, passed to the target suite only.
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
        self.stamp = datetime.now(UTC).strftime(TIMESTAMP_FORMAT)
        self.commit = None
        self.config = None
        self._driver = None
        self._ota = None
        self._install_slot = None
        self._bundle_sha256 = None
        self._target_run_dir = None

    def resolve_and_checkout(self):
        """Resolve `self.ref` to a commit, refuse a dirty tree, and check out that commit.

        git's own stderr is never captured, so it reaches the terminal directly.

        Returns:
            str: The resolved commit hex, also stored on `self.commit`.

        Raises:
            RuntimeError: The tree has modified or staged tracked files.
            subprocess.CalledProcessError: The ref did not resolve, or the checkout failed.
        """
        rev_parse = subprocess.run(
            ["git", "rev-parse", "--verify", f"{self.ref}^{{commit}}"],
            cwd=self.checkout,
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        )
        self.commit = rev_parse.stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.checkout,
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        )
        dirty = [line for line in status.stdout.splitlines() if not line.startswith("??")]
        if dirty:
            raise RuntimeError(f"tree is dirty: {dirty}")
        subprocess.run(["git", "checkout", self.commit], cwd=self.checkout, check=True)
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

        Output is written to the terminal and to `<result_base>/<stamp>/<name>.log`, each
        flushed per line so neither lags behind a following step's own output. A non-UTF-8
        byte in the hook's output is replaced rather than raising mid-stream.

        Args:
            name (str): One of `wisebench.config.HOOK_NAMES`.

        Returns:
            int: The hook's exit status.

        Raises:
            OSError: The log file could not be opened.
        """
        log_dir = self.result_base / self.stamp
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / f"{name}.log", "w", encoding="utf-8", buffering=1) as log:
            process = subprocess.Popen(
                self.config.hooks[name],
                shell=True,
                cwd=self.checkout,
                env=self._subprocess_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
            return process.wait()

    def recover(self):
        """Build this run's `Ota`, undo a mid-OTA crash, then deactivate the labgrid driver.

        Builds the labgrid `Target`/`SSHDriver` from `self.lg_env` through labgrid's own
        `Environment` API (`Environment(config_file=...).get_target("main").get_driver(
        "SSHDriver")`), and constructs the one `Ota` instance `install` and `rollback` reuse.
        Deactivates the driver once `Ota.recover()` returns -- see the class docstring for why;
        `install` reactivates it.

        Raises:
            Exception: labgrid could not build the target or the driver.
            OtaError: A slot-status read or mark-bad exited nonzero, or the board did not
                come back on the expected slot.
        """
        target = Environment(config_file=self.lg_env).get_target("main")
        self._driver = target.get_driver("SSHDriver")
        self._ota = Ota(self._driver, self.result_base / "mid-ota")
        self._ota.recover()
        target.deactivate(self._driver)

    def _run_pytest(self, suite, *, lg_env=False, extra_args=()):
        """Run pytest on one suite directory and locate the `run.json` it wrote.

        Runs under this process's own interpreter (`sys.executable -m pytest`, not `uv run`,
        since the checkout at `self.checkout` may hold its own project) with `-o addopts=""`,
        clearing that checkout's own ini-file `addopts` for this invocation (its `testpaths`,
        `pythonpath` and markers still apply).

        Args:
            suite (str): The suite's path, relative to the checkout.
            lg_env (bool): Whether to pass `--lg-env` (the target suite).
            extra_args (tuple[str, ...]): Extra pytest arguments, appended last.

        Returns:
            tuple[int, pathlib.Path]: pytest's exit status, and the directory its `run.json`
            landed in.

        Raises:
            RuntimeError: Zero or more than one new directory appeared under
                `self.result_base`, so which one is this invocation's `run.json` is
                ambiguous.
            OSError: `self.result_base` could not be listed, or the pytest subprocess could
                not be started.
        """
        before = set(self.result_base.iterdir()) if self.result_base.exists() else set()
        command = [sys.executable, "-m", "pytest", suite, "-o", "addopts="]
        if lg_env:
            command += ["--lg-env", self.lg_env]
        command += list(extra_args)
        result = subprocess.run(command, cwd=self.checkout, env=self._subprocess_env())
        after = set(self.result_base.iterdir())
        new_dirs = [p for p in after - before if p.is_dir()]
        if len(new_dirs) != 1:
            raise RuntimeError(
                f"expected exactly one new directory under {self.result_base}, found {new_dirs}"
            )
        return result.returncode, new_dirs[0]

    def run_host_suite(self):
        """Run pytest on the host suite and record ref/commit/set into its `run.json`.

        Returns:
            int: pytest's exit status.

        Raises:
            RuntimeError: `_run_pytest` could not find its own result directory.
            OSError: `_run_pytest`'s subprocess could not be started, or `run.json` could not
                be read or written.
            json.JSONDecodeError: `run.json` is not valid JSON.
        """
        returncode, run_dir = self._run_pytest(self.config.host_suite)
        self._merge_run_json(run_dir, ref=self.ref, commit=self.commit, set=self.set_name)
        return returncode

    def install(self):
        """Reactivate the driver, install the configured bundle, and boot into it.

        Stores the booted slot and the bundle's sha256 on the instance for `run_target_suite`
        and `rollback` to record.

        Raises:
            OSError: The bundle could not be opened.
            OtaError: `rauc install` or a slot-status read exited nonzero.
            Exception: ssh did not answer again within the wait bound.
        """
        self._driver.target.activate(self._driver)
        bundle = self.checkout / self.config.bundle_path
        self._install_slot, self._bundle_sha256 = self._ota.install(
            bundle, self.config.bundle_destination
        )

    def run_target_suite(self):
        """Run pytest on the target suite with `--lg-env` and the trailing pytest arguments.

        Records ref/commit/set, the bundle's sha256 and the slot `install` booted into
        `run.json` before returning, so this suite's provenance survives even if the
        following `rollback` raises; `rollback` adds the slot it booted back to once it runs.

        Returns:
            int: pytest's exit status.

        Raises:
            RuntimeError: `_run_pytest` could not find its own result directory.
            OSError: `_run_pytest`'s subprocess could not be started, or `run.json` could not
                be read or written.
            json.JSONDecodeError: `run.json` is not valid JSON.
        """
        returncode, run_dir = self._run_pytest(
            self.config.target_suite, lg_env=True, extra_args=self.pytest_args
        )
        self._merge_run_json(
            run_dir,
            ref=self.ref,
            commit=self.commit,
            set=self.set_name,
            bundle_sha256=self._bundle_sha256,
            slots={"after_install": self._install_slot},
        )
        self._target_run_dir = run_dir
        return returncode

    def rollback(self):
        """Roll back to the previous slot, and add it to the target suite's `run.json`.

        Returns:
            str: The booted slot after the rollback.

        Raises:
            RuntimeError: `install` has not run.
            OtaError: `rauc status mark-bad` or a slot-status read exited nonzero, or the
                board did not come back on the previous slot.
            Exception: ssh did not answer again within the wait bound.
        """
        slot = self._ota.rollback()
        if self._target_run_dir is not None:
            self._merge_run_json(
                self._target_run_dir,
                slots={"after_install": self._install_slot, "after_rollback": slot},
            )
        return slot

    def _merge_run_json(self, run_dir, **fields):
        """Merge fields into one pytest invocation's `run.json`.

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

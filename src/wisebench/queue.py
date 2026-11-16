"""Queue: runs queued by hand in `records/queue.db`, then the GitHub merge queue's entries."""

import contextlib
import json
import os
import signal
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

from wisebench.bench_test import BenchFailure
from wisebench.check import Check
from wisebench.config import Config

_NETWORK_TIMEOUT = 30


class Queue:
    """Runs queued by hand, then the GitHub merge queue's entries, for one checkout.

    Hand entries live in the `queue` table of `<directory>/queue.db`; the merge queue is read
    from GitHub's GraphQL API through `Check`, built from the `[github]` table of the checkout's
    `bench.toml`. `entries()` defines the run order and `take()` claims its first entry.
    """

    def __init__(self, checkout, directory):
        """Bind one Queue to a checkout and the directory holding `queue.db`.

        Args:
            checkout (pathlib.Path): The layer's clone `git fetch` and `git rev-parse` run in,
                and whose `bench.toml` names the repository the merge queue is read from.
            directory (pathlib.Path): The records directory `queue.db` sits in; created on first
                use.
        """
        self.checkout = checkout
        self.directory = directory

    def _db(self):
        """Open a connection to `queue.db`, creating the directory, file and table if absent.

        Returns:
            sqlite3.Connection: In autocommit mode, in WAL mode; the caller closes it.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.directory / "queue.db", timeout=30, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            "CREATE TABLE IF NOT EXISTS queue (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "added TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')), "
            "entry TEXT NOT NULL)"
        )
        return db

    def _git(self, args, network=False, cwd=None):
        """Run one git command, silent and detached from the terminal.

        Args:
            args (list[str]): The command line after `git`.
            cwd (pathlib.Path | None): Where to run it; None runs it in the checkout.
            network (bool): Whether the command reaches `origin`. A network call is bounded by
                `_NETWORK_TIMEOUT`; a timeout kills git and every process it started. Its failure
                or timeout is raised as `BenchFailure("git <command>: <first stderr line>")`.

        Returns:
            str: The command's standard output.

        Raises:
            BenchFailure: A network call failed or timed out.
            subprocess.CalledProcessError: Any other call failed.
        """
        with subprocess.Popen(
            ["git", *args],
            cwd=cwd or self.checkout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        ) as process:
            try:
                out, err = process.communicate(timeout=_NETWORK_TIMEOUT if network else None)
            except subprocess.TimeoutExpired as exc:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise BenchFailure(f"git {args[0]}: timed out after {_NETWORK_TIMEOUT}s") from exc
        if process.returncode:
            if network:
                lines = err.strip().splitlines()
                raise BenchFailure(
                    f"git {args[0]}: {lines[0] if lines else f'exit status {process.returncode}'}"
                )
            raise subprocess.CalledProcessError(process.returncode, ["git", *args], out, err)
        return out

    @staticmethod
    def _source_dir(source):
        """The directory `source` names: `~` expanded, made absolute against the working directory.

        Args:
            source (str | None): The directory as typed, or None.

        Returns:
            pathlib.Path | None: The absolute directory; None when `source` is None.
        """
        if source is None:
            return None
        return Path(os.path.abspath(os.path.expanduser(source)))

    def _resolve(self, ref, source=None):
        """Resolve `ref` to the commit sha it names.

        Args:
            ref (str): The git ref, as typed.
            source (pathlib.Path | None): The directory to resolve in; None resolves in the
                checkout.

        Returns:
            str: The commit sha.

        Raises:
            subprocess.CalledProcessError: `ref` does not resolve to a commit there.
        """
        return self._git(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=source).strip()

    def preview(self, ref, source=None):
        """Describe what queueing `ref` would run, without queueing it.

        Args:
            ref (str): The git ref, as typed.
            source (str | None): A directory to resolve `ref` in, as typed (`~` is expanded and a
                relative path is taken against the working directory); nothing is fetched from
                it. None resolves in the checkout.

        Returns:
            dict: `{sha, subject, clean}`: the commit `ref` resolves to, its subject line, and
            whether the checkout, where the run builds, has no uncommitted change to a tracked
            file (untracked files are ignored).

        Raises:
            subprocess.CalledProcessError: `ref` does not resolve to a commit there.
        """
        directory = self._source_dir(source)
        sha = self._resolve(ref, directory)
        subject = self._git(["log", "-1", "--format=%s", sha], cwd=directory).strip()
        status = self._git(["--no-optional-locks", "status", "--porcelain", "--untracked-files=no"])
        return {"sha": sha, "subject": subject, "clean": not status.strip()}

    def add(self, ref, set_name, skip_build, pytest_args, source=None):
        """Queue a run of `ref` by hand, pinned to the commit `ref` names now.

        With `source`, `ref` resolves there and its commit is fetched into the checkout, writing
        objects only: no ref and no `FETCH_HEAD`, so it cannot contend with `take()`'s own fetch.

        Args:
            ref (str): The git ref to run, as typed.
            set_name (str | None): The replay set name, or None.
            skip_build (bool): Whether the run skips `build`, `bundle` and the host suite.
            pytest_args (list[str]): Arguments for the target suite.
            source (str | None): A directory to resolve `ref` in, as typed (`~` is expanded and a
                relative path is taken against the working directory); None resolves in the
                checkout.

        Returns:
            tuple[int, str, int]: The new entry's id, the commit sha `ref` resolved to, and its
            place: the number of hand entries queued up to and including this one.

        Raises:
            subprocess.CalledProcessError: `ref` does not resolve to a commit there.
            BenchFailure: Fetching the commit from `source` failed or timed out.
        """
        directory = self._source_dir(source)
        sha = self._resolve(ref, directory)
        if directory is not None:
            self._git(["fetch", "--no-write-fetch-head", str(directory), sha], network=True)
        entry = {
            "ref": ref,
            "sha": sha,
            "set": set_name,
            "skip_build": skip_build,
            "args": pytest_args,
        }
        with closing(self._db()) as db:
            entry_id = db.execute(
                "INSERT INTO queue (entry) VALUES (?)", (json.dumps(entry),)
            ).lastrowid
            (place,) = db.execute(
                "SELECT COUNT(*) FROM queue WHERE id <= ?", (entry_id,)
            ).fetchone()
        return entry_id, sha, place

    def entries(self):
        """List what waits, in run order: hand entries oldest first, then the merge queue.

        Reads the merge queue from GitHub; fetches nothing.

        Returns:
            list[dict]: A hand entry is `{kind: "test", id, added, ref, sha, set, skip_build,
            args}`; a merge-queue entry is `{kind: "run", pr, title, sha}`, in GitHub's order,
            `sha` None while GitHub has not built it.

        Raises:
            OSError: `<checkout>/bench.toml` could not be read.
            ValueError: `bench.toml` has no complete `[github]` table, or the repository has no
                merge queue on `main`.
            github.GithubException: PyGithub's own call failed.
            jwt.exceptions.InvalidKeyError: The App's private key is malformed.
            sqlite3.DatabaseError: `queue.db` is unreadable.
        """
        with closing(self._db()) as db:
            rows = db.execute("SELECT id, added, entry FROM queue ORDER BY id").fetchall()
        entries = [
            {"kind": "test", "id": id_, "added": added, **json.loads(entry)}
            for id_, added, entry in rows
        ]
        config = Config.load(self.checkout / "bench.toml")
        check = Check(
            config.github_repo,
            config.github_app_id,
            config.github_private_key,
            config.github_check,
        )
        entries.extend({"kind": "run", **entry} for entry in check.merge_queue())
        return entries

    def remove(self, entry_id):
        """Delete one hand entry; of concurrent callers for the same id exactly one gets True.

        Args:
            entry_id (int): The id `add` returned.

        Returns:
            bool: True if this call deleted the entry; False if it was never queued, was removed,
            or was already taken.
        """
        with closing(self._db()) as db:
            return db.execute("DELETE FROM queue WHERE id = ?", (entry_id,)).rowcount == 1

    def take(self):
        """Claim the first waiting entry: the oldest hand entry, else the first built merge entry.

        Reads the queue, then fetches `origin`, which brings that commit; the read comes first so an
        entry GitHub builds meanwhile is never read without its commit.

        A hand entry is deleted when claimed, so it is never returned again whatever its run's
        outcome; one removed in the meantime is skipped.

        Returns:
            dict | None: The claimed entry as `entries()` lists it, or None when nothing waits.

        Raises:
            BenchFailure: `git fetch` failed or timed out.
            OSError: `<checkout>/bench.toml` could not be read.
            ValueError: `bench.toml` has no complete `[github]` table, or the repository has no
                merge queue on `main`.
            github.GithubException: PyGithub's own call failed.
            jwt.exceptions.InvalidKeyError: The App's private key is malformed.
            sqlite3.DatabaseError: `queue.db` is unreadable.
        """
        entries = self.entries()
        self._git(["fetch", "origin"], network=True)
        for entry in entries:
            if entry["kind"] == "run":
                if entry["sha"] is not None:
                    return entry
            elif self.remove(entry["id"]):
                return entry
        return None

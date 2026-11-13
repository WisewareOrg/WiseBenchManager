"""Queue: runs queued by hand in `records/queue.db`, then the merge queue's head entry."""

import json
import os
import re
import sqlite3
import subprocess
from contextlib import closing

from wisebench.bench_test import BenchFailure

_REF_RE = re.compile(r"pr-(\d+)-([0-9a-f]+)")
_NETWORK_TIMEOUT = 30


class Queue:
    """Runs queued by hand, then the GitHub merge queue's head entry, for one checkout.

    Hand entries live in the `queue` table of `<directory>/queue.db`; the merge queue's head is
    read with `git ls-remote` against the checkout. `entries()` defines the run order and `take()`
    claims its first entry; `head()` is swappable for a different queue adapter (e.g. Mergify)
    behind the same signature.
    """

    def __init__(self, checkout, directory):
        """Bind one Queue to a checkout and the directory holding `queue.db`.

        Args:
            checkout (pathlib.Path): The layer's clone `git fetch`, `git ls-remote` and
                `git rev-parse` run in.
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

    def _git(self, args, network=False):
        """Run one git command in the checkout, silent and detached from the terminal.

        Args:
            args (list[str]): The command line after `git`.
            network (bool): Whether the command reaches `origin`. A network call is bounded by a
                30 second timeout, and its failure or timeout is raised as
                `BenchFailure("git <command>: <first stderr line>")`.

        Returns:
            subprocess.CompletedProcess: Text output captured; stdout in `.stdout`.

        Raises:
            BenchFailure: A network call failed or timed out.
            subprocess.CalledProcessError: Any other call failed.
        """
        try:
            return subprocess.run(
                ["git", *args],
                cwd=self.checkout,
                capture_output=True,
                text=True,
                check=True,
                start_new_session=True,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                **({"timeout": _NETWORK_TIMEOUT} if network else {}),
            )
        except subprocess.TimeoutExpired as exc:
            if network:
                raise BenchFailure(f"git {args[0]}: timed out after {_NETWORK_TIMEOUT}s") from exc
            raise
        except subprocess.CalledProcessError as exc:
            if network:
                lines = (exc.stderr or "").strip().splitlines()
                reason = lines[0] if lines else str(exc)
                raise BenchFailure(f"git {args[0]}: {reason}") from exc
            raise

    def _resolve(self, ref):
        """Resolve `ref` to the commit sha it names.

        Args:
            ref (str): The git ref, as typed.

        Returns:
            str: The commit sha.

        Raises:
            subprocess.CalledProcessError: `ref` does not resolve to a commit in the checkout.
        """
        return self._git(["rev-parse", "--verify", f"{ref}^{{commit}}"]).stdout.strip()

    def preview(self, ref):
        """Describe what queueing `ref` would run, without queueing it.

        Args:
            ref (str): The git ref, as typed.

        Returns:
            dict: `{sha, subject, clean}`: the commit `ref` resolves to, its subject line, and
            whether the checkout has no uncommitted change to a tracked file (untracked files
            are ignored).

        Raises:
            subprocess.CalledProcessError: `ref` does not resolve to a commit in the checkout.
        """
        sha = self._resolve(ref)
        subject = self._git(["log", "-1", "--format=%s", sha]).stdout.strip()
        status = self._git(
            ["--no-optional-locks", "status", "--porcelain", "--untracked-files=no"]
        ).stdout
        return {"sha": sha, "subject": subject, "clean": not status.strip()}

    def add(self, ref, set_name, skip_build, pytest_args):
        """Queue a run of `ref` by hand, pinned to the commit `ref` names now.

        Args:
            ref (str): The git ref to run, as typed.
            set_name (str | None): The replay set name, or None.
            skip_build (bool): Whether the run skips `build`, `bundle` and the host suite.
            pytest_args (list[str]): Arguments for the target suite.

        Returns:
            tuple[int, str, int]: The new entry's id, the commit sha `ref` resolved to, and its
            place: the number of hand entries queued up to and including this one.

        Raises:
            subprocess.CalledProcessError: `ref` does not resolve to a commit in the checkout.
        """
        sha = self._resolve(ref)
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
        """List what waits, in run order: hand entries oldest first, then the merge queue's head.

        Runs `head()`, which reads `origin` but fetches nothing.

        Returns:
            list[dict]: A hand entry is `{kind: "test", id, added, ref, sha, set, skip_build,
            args}`; the merge head is `{kind: "run", ref, sha, pr}`.

        Raises:
            BenchFailure: More than one merge-queue ref qualifies, or `git ls-remote` failed or
                timed out.
            sqlite3.DatabaseError: `queue.db` is unreadable.
        """
        with closing(self._db()) as db:
            rows = db.execute("SELECT id, added, entry FROM queue ORDER BY id").fetchall()
        entries = [
            {"kind": "test", "id": id_, "added": added, **json.loads(entry)}
            for id_, added, entry in rows
        ]
        head = self.head()
        if head is not None:
            entries.append({"kind": "run", **head})
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
        """Claim the first waiting entry: the oldest hand entry, else the merge queue's head.

        Fetches `origin` first, so `origin/*` is fresh for the run that follows.

        A hand entry is deleted when claimed, so it is never returned again whatever its run's
        outcome; one removed in the meantime is skipped.

        Returns:
            dict | None: The claimed entry as `entries()` lists it, or None when nothing waits.

        Raises:
            BenchFailure: More than one merge-queue ref qualifies, or `git fetch` or `git
                ls-remote` failed or timed out.
            sqlite3.DatabaseError: `queue.db` is unreadable.
        """
        self._git(["fetch", "origin"], network=True)
        for entry in self.entries():
            if entry["kind"] == "run" or self.remove(entry["id"]):
                return entry
        return None

    def head(self):
        """Read the queue entry whose own base is `origin/main`'s current tip.

        Reads `main`'s tip and `refs/heads/gh-readonly-queue/main/*` from `origin` with
        `git ls-remote`, fetching nothing, and keeps the refs named `pr-<PR_NUMBER>-<BASE_SHA>`
        whose `<BASE_SHA>` equals that tip; a stale entry (based on an earlier tip) reads as no
        job, the same as an empty queue.

        Returns:
            dict | None: `{ref, sha, pr}`: the ref name, the ref's own sha (the commit to build,
            from `git ls-remote`'s own column, not the ref name) and the PR number the ref names,
            or None when no ref qualifies.

        Raises:
            BenchFailure: More than one ref qualifies, `origin` has no `refs/heads/main`, or
                `git ls-remote` failed or timed out.
        """
        main_lines = self._git(
            ["ls-remote", "origin", "refs/heads/main"], network=True
        ).stdout.splitlines()
        main = next(
            (line.split("\t")[0] for line in main_lines if line.endswith("\trefs/heads/main")),
            None,
        )
        if main is None:
            raise BenchFailure("origin has no refs/heads/main")
        remote = self._git(
            ["ls-remote", "origin", "refs/heads/gh-readonly-queue/main/*"], network=True
        ).stdout

        matches = []
        for line in remote.splitlines():
            if not line:
                continue
            sha, ref = line.split("\t")
            name = ref.rsplit("/", 1)[-1]
            match = _REF_RE.fullmatch(name)
            if match and match.group(2) == main:
                matches.append({"ref": ref, "sha": sha, "pr": int(match.group(1))})

        if not matches:
            return None
        if len(matches) > 1:
            raise BenchFailure("more than one gh-readonly-queue ref based on origin/main")
        return matches[0]

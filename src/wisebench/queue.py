"""Queue: the merge queue's head entry for one checkout, read with `git ls-remote`."""

import re
import subprocess

from wisebench.bench_test import BenchFailure

_REF_RE = re.compile(r"pr-(\d+)-([0-9a-f]+)")


class Queue:
    """The GitHub merge queue's head entry, read with `git ls-remote` against one checkout.

    Holds the checkout `git fetch`, `git ls-remote` and `git rev-parse` run in; `head()` is the
    whole mechanism, swappable for a different queue adapter (e.g. Mergify) behind the same
    signature.
    """

    def __init__(self, checkout):
        """Bind one Queue to a checkout.

        Args:
            checkout (pathlib.Path): The layer's clone `git fetch`, `git ls-remote` and
                `git rev-parse` run in.
        """
        self.checkout = checkout

    def head(self):
        """Read the queue entry whose own base is `origin/main`'s current tip.

        Reads `refs/heads/gh-readonly-queue/main/*` and keeps the refs named
        `pr-<PR_NUMBER>-<BASE_SHA>` whose `<BASE_SHA>` equals `origin/main`'s current tip; a
        stale entry (based on an earlier tip) reads as no job, the same as an empty queue.

        Returns:
            tuple[str, str, int] | None: The ref name, the ref's own sha (the commit to build,
            from `git ls-remote`'s own column, not the ref name) and the PR number, or None when
            no ref qualifies.

        Raises:
            BenchFailure: More than one ref qualifies.
            subprocess.CalledProcessError: `git fetch`, `git ls-remote` or `git rev-parse`
                failed.
        """
        subprocess.run(["git", "fetch", "origin"], cwd=self.checkout, check=True)
        main = subprocess.run(
            ["git", "rev-parse", "origin/main"],
            cwd=self.checkout,
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        remote = subprocess.run(
            ["git", "ls-remote", "origin", "refs/heads/gh-readonly-queue/main/*"],
            cwd=self.checkout,
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout

        matches = []
        for line in remote.splitlines():
            if not line:
                continue
            sha, ref = line.split("\t")
            name = ref.rsplit("/", 1)[-1]
            match = _REF_RE.fullmatch(name)
            if match and match.group(2) == main:
                matches.append((ref, sha, int(match.group(1))))

        if not matches:
            return None
        if len(matches) > 1:
            raise BenchFailure("more than one gh-readonly-queue ref based on origin/main")
        return matches[0]

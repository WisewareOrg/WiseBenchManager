"""Check: posts a run's verdict as a GitHub check run and reads the merge queue, as a GitHub App."""

from pathlib import Path

from github import Auth, GithubIntegration

_CONCLUSIONS = ("success", "failure")
_MERGE_QUEUE = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    mergeQueue(branch: "main") {
      entries(first: 50) {
        nodes {
          pullRequest { number title }
          headCommit { oid }
        }
      }
    }
  }
}
"""


class Check:
    """One check run per `post`, and the merge queue by `merge_queue`, as a GitHub App.

    The App is installed on one repository.

    Holds the repository's owner and name, split from "owner/name", the app's authentication
    and the check run's name; calls PyGithub's `GithubIntegration` and the objects it returns,
    never re-exposing them.
    """

    def __init__(self, repo, app_id, private_key, check):
        """Bind one Check to a repository, a GitHub App and the check run's name.

        Args:
            repo (str): The repository the verdict is posted against, as "owner/name".
            app_id (int): The GitHub App's id.
            private_key (str | os.PathLike): Path to the App's PEM-encoded private key; `~` is
                expanded.
            check (str): The check run's name, as the layer's ruleset lists it.

        Raises:
            ValueError: `repo`, `app_id`, `private_key` or `check` is `None` (the `[github]`
                table was absent from the settings file), or `repo` is not exactly one
                "/"-separated owner and name.
            OSError: `private_key` could not be read.
            AssertionError: `private_key`'s contents are empty.
        """
        for name, value in (
            ("repo", repo),
            ("app_id", app_id),
            ("private_key", private_key),
            ("check", check),
        ):
            if value is None:
                raise ValueError(f"[github] {name} is required")
        self._owner, self._name = repo.split("/")
        self._check = check
        with Path(private_key).expanduser().open() as f:
            self._auth = Auth.AppAuth(app_id, f.read())

    def _github(self):
        """The `Github` authenticated as the App's installation on the repository."""
        integration = GithubIntegration(auth=self._auth)
        installation = integration.get_repo_installation(self._owner, self._name)
        return installation.get_github_for_installation()

    def merge_queue(self):
        """Read the repository's merge queue for `main` from GitHub's GraphQL API.

        Authenticates as the App's installation on the repository, then reads the first 50
        entries.

        Returns:
            list[dict]: `{pr, title, sha}` per entry, in GitHub's order; `sha` is the merge
            group's commit, None while GitHub has not built it.

        Raises:
            ValueError: The repository has no merge queue on `main`.
            github.GithubException: PyGithub's own call failed.
            jwt.exceptions.InvalidKeyError: the App's private key is malformed.
        """
        _, data = self._github().requester.graphql_query(
            _MERGE_QUEUE, {"owner": self._owner, "name": self._name}
        )
        queue = data["data"]["repository"]["mergeQueue"]
        if queue is None:
            raise ValueError(f"{self._owner}/{self._name} has no merge queue on main")
        nodes = queue["entries"]["nodes"]
        return [
            {
                "pr": node["pullRequest"]["number"],
                "title": node["pullRequest"]["title"],
                "sha": node["headCommit"]["oid"] if node["headCommit"] else None,
            }
            for node in nodes
        ]

    def post(self, sha, conclusion, title, summary):
        """Create one completed check run on a commit.

        Authenticates as the App's installation on the repository, then creates the check run
        under that installation's token.

        Args:
            sha (str): The commit's full sha the check run is posted against.
            conclusion (str): "success" or "failure".
            title (str): The one-line verdict.
            summary (str): The report directory path.

        Raises:
            ValueError: `conclusion` is neither "success" nor "failure".
            github.GithubException: PyGithub's own call failed.
            jwt.exceptions.InvalidKeyError: the App's private key is malformed.
        """
        if conclusion not in _CONCLUSIONS:
            raise ValueError(f"conclusion must be 'success' or 'failure', got {conclusion!r}")
        gh = self._github()
        gh.get_repo(f"{self._owner}/{self._name}").create_check_run(
            name=self._check,
            head_sha=sha,
            status="completed",
            conclusion=conclusion,
            output={"title": title, "summary": summary},
        )

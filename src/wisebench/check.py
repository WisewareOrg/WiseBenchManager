"""Check: posts a run's verdict as a GitHub check run, authenticated as a GitHub App."""

from github import Auth, GithubIntegration

_CONCLUSIONS = ("success", "failure")


class Check:
    """One check run per `post`, authenticated as a GitHub App installed on one repository.

    Holds the repository's owner and name, split from "owner/name", the app's authentication
    and the check run's name; calls PyGithub's `GithubIntegration` and the objects it returns,
    never re-exposing them.
    """

    def __init__(self, repo, app_id, private_key, check):
        """Bind one Check to a repository, a GitHub App and the check run's name.

        Args:
            repo (str): The repository the verdict is posted against, as "owner/name".
            app_id (int): The GitHub App's id.
            private_key (str | os.PathLike): Path to the App's PEM-encoded private key.
            check (str): The check run's name, as the layer's ruleset lists it.

        Raises:
            ValueError: `repo` is not exactly one "/"-separated owner and name.
            OSError: `private_key` could not be read.
            AssertionError: `private_key`'s contents are empty.
        """
        self._owner, self._name = repo.split("/")
        self._check = check
        with open(private_key) as f:
            self._auth = Auth.AppAuth(app_id, f.read())

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
        integration = GithubIntegration(auth=self._auth)
        installation = integration.get_repo_installation(self._owner, self._name)
        gh = installation.get_github_for_installation()
        gh.get_repo(f"{self._owner}/{self._name}").create_check_run(
            name=self._check,
            head_sha=sha,
            status="completed",
            conclusion=conclusion,
            output={"title": title, "summary": summary},
        )

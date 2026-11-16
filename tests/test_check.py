"""`Check.post`: one completed check run through PyGithub's app authentication."""

import pytest
from github import Auth, Github, GithubIntegration, Repository
from github.Installation import Installation
from github.Requester import Requester

from wisebench import check


class FakeRepository:
    """Records every `create_check_run` call."""

    def __init__(self):
        self.calls = []

    def create_check_run(self, **kwargs):
        self.calls.append(kwargs)


class FakeRequester:
    """Records every `graphql_query` call and answers `(headers, data)` with `data`."""

    def __init__(self):
        self.data = {}
        self.calls = []

    def graphql_query(self, query, variables):
        self.calls.append((query, variables))
        return {}, self.data


class FakeGithub:
    """Records every `get_repo` call; `get_repo` returns the one `FakeRepository`."""

    def __init__(self, repository):
        self._repository = repository
        self.get_repo_calls = []
        self.requester = FakeRequester()

    def get_repo(self, full_name_or_id):
        self.get_repo_calls.append(full_name_or_id)
        return self._repository


class FakeInstallation:
    """`get_github_for_installation` returns a `FakeGithub` over the one `FakeRepository`."""

    def __init__(self, repository):
        self._repository = repository
        self.github = FakeGithub(repository)

    def get_github_for_installation(self):
        return self.github


class FakeGithubIntegration:
    """Records its calls; `get_repo_installation` returns a fixed `FakeInstallation`."""

    instances = []

    def __init__(self, *, auth):
        self.auth = auth
        self.repository = FakeRepository()
        self.installation = FakeInstallation(self.repository)
        self.get_repo_installation_calls = []
        FakeGithubIntegration.instances.append(self)

    def get_repo_installation(self, owner, name):
        self.get_repo_installation_calls.append((owner, name))
        return self.installation


@pytest.fixture
def private_key_path(tmp_path):
    path = tmp_path / "app.pem"
    path.write_text("not-a-real-key")
    return path


def test_pygithub_app_auth_chain_exists_on_the_installed_version():
    assert hasattr(Auth, "AppAuth")
    assert hasattr(GithubIntegration, "get_repo_installation")
    assert hasattr(Installation, "get_github_for_installation")
    assert hasattr(Github, "get_repo")
    assert hasattr(Repository.Repository, "create_check_run")
    assert hasattr(Github, "requester")
    assert hasattr(Requester, "graphql_query")


def test_init_raises_oserror_when_the_private_key_path_is_missing(tmp_path):
    with pytest.raises(OSError):
        check.Check("acme/layer", 1, tmp_path / "missing.pem", "bench")


def test_init_expands_a_leading_tilde_in_the_private_key_path(tmp_path):
    (tmp_path / "app.pem").write_text("not-a-real-key")
    check.Check("acme/layer", 1, "~/app.pem", "bench")


@pytest.mark.parametrize(
    ("repo", "app_id", "private_key", "check_name", "missing"),
    [
        (None, 1, "key", "bench", "repo"),
        ("acme/layer", None, "key", "bench", "app_id"),
        ("acme/layer", 1, None, "bench", "private_key"),
        ("acme/layer", 1, "key", None, "check"),
    ],
)
def test_init_raises_valueerror_naming_a_none_argument(
    repo, app_id, private_key, check_name, missing
):
    with pytest.raises(ValueError, match=f"\\[github\\] {missing} is required"):
        check.Check(repo, app_id, private_key, check_name)


@pytest.mark.parametrize(
    ("repo", "match"),
    [
        ("acme-layer", "not enough values to unpack"),
        ("acme/layer/extra", "too many values to unpack"),
    ],
)
def test_init_raises_valueerror_when_repo_is_not_owner_slash_name(private_key_path, repo, match):
    with pytest.raises(ValueError, match=match):
        check.Check(repo, 1, private_key_path, "bench")


def test_post_creates_one_completed_check_run(monkeypatch, private_key_path):
    FakeGithubIntegration.instances = []
    monkeypatch.setattr(check, "GithubIntegration", FakeGithubIntegration)
    one = check.Check("acme/layer", 1, private_key_path, "a-different-check-name")
    one.post("deadbeef", "success", "PASS", "records/run")

    integration = FakeGithubIntegration.instances[0]
    assert integration.auth.app_id == "1"
    assert integration.get_repo_installation_calls == [("acme", "layer")]
    assert integration.installation.github.get_repo_calls == ["acme/layer"]
    assert integration.repository.calls == [
        {
            "name": "a-different-check-name",
            "head_sha": "deadbeef",
            "status": "completed",
            "conclusion": "success",
            "output": {"title": "PASS", "summary": "records/run"},
        }
    ]


@pytest.mark.parametrize("conclusion", ["pending", "neutral", "skipped"])
def test_post_raises_valueerror_on_an_invalid_conclusion(monkeypatch, private_key_path, conclusion):
    monkeypatch.setattr(check, "GithubIntegration", FakeGithubIntegration)
    one = check.Check("acme/layer", 1, private_key_path, "bench")
    with pytest.raises(ValueError, match="success' or 'failure'"):
        one.post("deadbeef", conclusion, "title", "summary")


@pytest.mark.parametrize("conclusion", ["pending", "neutral", "skipped"])
def test_post_never_calls_github_integration_on_an_invalid_conclusion(
    monkeypatch, private_key_path, conclusion
):
    def boom(*args, **kwargs):
        raise AssertionError("GithubIntegration must not be constructed")

    monkeypatch.setattr(check, "GithubIntegration", boom)
    one = check.Check("acme/layer", 1, private_key_path, "bench")
    with pytest.raises(ValueError):
        one.post("deadbeef", conclusion, "title", "summary")


def _merge_queue_data(*nodes):
    return {"repository": {"mergeQueue": {"entries": {"nodes": list(nodes)}}}}


def test_merge_queue_returns_each_entry_in_githubs_order_with_none_for_an_unbuilt_one(
    monkeypatch, private_key_path
):
    FakeGithubIntegration.instances = []
    monkeypatch.setattr(check, "GithubIntegration", FakeGithubIntegration)
    one = check.Check("acme/layer", 1, private_key_path, "bench")
    requester = None

    def entry(position, number, title, oid):
        return {
            "position": position,
            "state": "QUEUED",
            "pullRequest": {"number": number, "title": title},
            "headCommit": {"oid": oid} if oid else None,
        }

    original = FakeGithubIntegration.get_repo_installation

    def capture(self, owner, name):
        installation = original(self, owner, name)
        installation.github.requester.data = _merge_queue_data(
            entry(1, 324, "Add slot check", "deadbeef"), entry(2, 326, "Fix probe", None)
        )
        return installation

    monkeypatch.setattr(FakeGithubIntegration, "get_repo_installation", capture)

    assert one.merge_queue() == [
        {"pr": 324, "title": "Add slot check", "sha": "deadbeef"},
        {"pr": 326, "title": "Fix probe", "sha": None},
    ]
    requester = FakeGithubIntegration.instances[0].installation.github.requester
    ((query, variables),) = requester.calls
    assert variables == {"o": "acme", "n": "layer"}
    assert 'mergeQueue(branch:"main")' in query
    assert "entries(first:50)" in query


def test_merge_queue_of_an_empty_queue_is_empty(monkeypatch, private_key_path):
    monkeypatch.setattr(check, "GithubIntegration", FakeGithubIntegration)
    original = FakeGithubIntegration.get_repo_installation

    def capture(self, owner, name):
        installation = original(self, owner, name)
        installation.github.requester.data = _merge_queue_data()
        return installation

    monkeypatch.setattr(FakeGithubIntegration, "get_repo_installation", capture)
    assert check.Check("acme/layer", 1, private_key_path, "bench").merge_queue() == []

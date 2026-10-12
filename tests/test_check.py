"""`Check.post`: one completed check run through PyGithub's app authentication."""

import pytest
from github import Auth, Github, GithubIntegration, Repository
from github.Installation import Installation

from wisebench import check


class FakeRepository:
    """Records every `create_check_run` call."""

    def __init__(self):
        self.calls = []

    def create_check_run(self, **kwargs):
        self.calls.append(kwargs)


class FakeGithub:
    """`get_repo` returns the one `FakeRepository`, regardless of the name asked for."""

    def __init__(self, repository):
        self._repository = repository

    def get_repo(self, full_name_or_id):
        return self._repository


class FakeInstallation:
    def __init__(self, installation_id):
        self.id = installation_id


class FakeGithubIntegration:
    """Records its calls; `get_repo_installation` and `get_github_for_installation` return
    fixed fakes."""

    instances = []

    def __init__(self, auth):
        self.auth = auth
        self.repository = FakeRepository()
        self.installation = FakeInstallation(4242)
        self.get_repo_installation_calls = []
        self.get_github_for_installation_calls = []
        FakeGithubIntegration.instances.append(self)

    def get_repo_installation(self, owner, name):
        self.get_repo_installation_calls.append((owner, name))
        return self.installation

    def get_github_for_installation(self, installation_id):
        self.get_github_for_installation_calls.append(installation_id)
        return FakeGithub(self.repository)


@pytest.fixture
def private_key_path(tmp_path):
    path = tmp_path / "app.pem"
    path.write_text("not-a-real-key")
    return path


def test_pygithub_app_auth_chain_exists_on_the_installed_version():
    assert hasattr(Auth, "AppAuth")
    assert hasattr(GithubIntegration, "get_repo_installation")
    assert hasattr(GithubIntegration, "get_github_for_installation")
    assert hasattr(Installation, "id")
    assert hasattr(Github, "get_repo")
    assert hasattr(Repository.Repository, "create_check_run")


def test_init_raises_oserror_when_the_private_key_path_is_missing(tmp_path):
    with pytest.raises(OSError):
        check.Check("acme/layer", 1, tmp_path / "missing.pem", "bench")


def test_post_creates_one_completed_check_run(monkeypatch, private_key_path):
    FakeGithubIntegration.instances = []
    monkeypatch.setattr(check, "GithubIntegration", FakeGithubIntegration)
    one = check.Check("acme/layer", 1, private_key_path, "a-different-check-name")
    one.post("deadbeef", "success", "PASS", "records/run")

    integration = FakeGithubIntegration.instances[0]
    assert integration.get_repo_installation_calls == [("acme", "layer")]
    assert integration.get_github_for_installation_calls == [4242]
    assert integration.repository.calls == [
        {
            "name": "a-different-check-name",
            "head_sha": "deadbeef",
            "status": "completed",
            "conclusion": "success",
            "output": {"title": "PASS", "summary": "records/run"},
        }
    ]


def test_post_raises_valueerror_on_an_invalid_conclusion(monkeypatch, private_key_path):
    monkeypatch.setattr(check, "GithubIntegration", FakeGithubIntegration)
    one = check.Check("acme/layer", 1, private_key_path, "bench")
    with pytest.raises(ValueError, match="success' or 'failure'"):
        one.post("deadbeef", "pending", "title", "summary")


def test_post_never_calls_github_integration_on_an_invalid_conclusion(
    monkeypatch, private_key_path
):
    def boom(*args, **kwargs):
        raise AssertionError("GithubIntegration must not be constructed")

    monkeypatch.setattr(check, "GithubIntegration", boom)
    one = check.Check("acme/layer", 1, private_key_path, "bench")
    with pytest.raises(ValueError):
        one.post("deadbeef", "pending", "title", "summary")

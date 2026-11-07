from github import Auth, Github, GithubIntegration
from github.Repository import Repository

from radar.config import Settings


def live_repo(s: Settings) -> Repository:
    integration = GithubIntegration(auth=Auth.AppAuth(s.github_app_id, s.github_app_key))
    owner, name = s.live_repo.split("/")
    installation = integration.get_repo_installation(owner, name)
    return installation.get_github_for_installation().get_repo(s.live_repo)


def before_repo(s: Settings) -> Repository:
    if not s.before_repo:
        raise ValueError("BEFORE_REPO is not set")
    gh = Github(auth=Auth.Token(s.github_token)) if s.github_token else Github()
    return gh.get_repo(s.before_repo)

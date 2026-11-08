import datetime as dt
import time
from collections import Counter
from functools import cache

from github import Auth, Github, GithubIntegration
from github.IssueEvent import IssueEvent
from github.PaginatedList import PaginatedList
from github.PullRequest import PullRequest
from github.Repository import Repository

from radar.config import Settings
from radar.core.models import PR

EXPERT_WINDOW_DAYS = 90
EXPERT_MAX_FILES = 15
EXPERT_CACHE_SECONDS = 1800
_expert_cache: dict[str, tuple[float, Counter]] = {}


@cache
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


def _requested_logins(p: PullRequest) -> set[str]:
    return {u.login.lower() for u in p.requested_reviewers}


def _requested_at(p: PullRequest, login: str) -> dt.datetime | None:
    """When `login` was last asked to review. Reads the events list directly: p.as_issue() needs the
    App's Issues permission, the events endpoint only needs Pull requests."""
    latest = None
    for e in PaginatedList(IssueEvent, p._requester, f"{p.issue_url}/events", None):
        reviewer = getattr(e, "requested_reviewer", None)
        if e.event == "review_requested" and reviewer and reviewer.login.lower() == login.lower():
            latest = e.created_at
    return latest


def request_times(p: PullRequest) -> dict[str, dt.datetime]:
    """When each currently requested reviewer (lowercase login) was last asked; PR creation if no event says."""
    requested = _requested_logins(p)
    if not requested:
        return {}
    times: dict[str, dt.datetime] = {}
    for e in PaginatedList(IssueEvent, p._requester, f"{p.issue_url}/events", None):
        reviewer = getattr(e, "requested_reviewer", None)
        if e.event == "review_requested" and reviewer and reviewer.login.lower() in requested:
            times[reviewer.login.lower()] = e.created_at
    return {login: times.get(login, p.created_at) for login in requested}


def open_prs(repo: Repository) -> list[PullRequest]:
    return list(repo.get_pulls(state="open"))


def commit_status(repo: Repository, sha: str, context: str) -> str | None:
    """Latest state of the `context` status on a commit, or None if it was never posted."""
    return next((st.state for st in repo.get_commit(sha).get_statuses() if st.context == context), None)


def open_review_counts(pulls: list[PullRequest]) -> Counter:
    return Counter(login for p in pulls for login in _requested_logins(p))


def waiting_on(pulls: list[PullRequest], lead: str) -> list[PR]:
    """Open, non-draft PRs where the lead is a requested reviewer (GitHub clears this once they review)."""
    result = []
    for p in pulls:
        if p.draft or lead.lower() not in _requested_logins(p):
            continue
        files = list(p.get_files())
        result.append(PR(
            number=p.number,
            title=p.title,
            url=p.html_url,
            author=p.user.login,
            head_sha=p.head.sha,
            waiting_since=_requested_at(p, lead) or p.created_at,
            files=tuple(f.filename for f in files),
            additions=p.additions,
            deletions=p.deletions,
            body=p.body or "",
            diff=build_diff(files),
        ))
    return result


def file_experts(repo: Repository, files: tuple[str, ...]) -> Counter:
    """Commits per author login (lowercase) touching any of `files` in the last 90 days on the default branch."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=EXPERT_WINDOW_DAYS)
    total: Counter = Counter()
    for path in files[:EXPERT_MAX_FILES]:
        cached = _expert_cache.get(path)
        if not cached or time.time() - cached[0] > EXPERT_CACHE_SECONDS:
            counts = Counter(c.author.login.lower() for c in repo.get_commits(path=path, since=since) if c.author)
            cached = _expert_cache[path] = (time.time(), counts)
        total += cached[1]
    return total


def still_waiting_on(repo: Repository, number: int, lead: str) -> PullRequest | None:
    p = repo.get_pull(number)
    if p.state != "open" or lead.lower() not in _requested_logins(p):
        return None
    return p


def hand_off(p: PullRequest, lead: str, reviewer: str, comment: str) -> None:
    p.create_review_request(reviewers=[reviewer])
    p.delete_review_request(reviewers=[lead])
    p.create_issue_comment(comment)


MAX_PATCH_CHARS = 3000
MAX_DIFF_CHARS = 12000


def build_diff(files: list) -> str:
    """files: PyGithub File objects."""
    parts = [f"--- {f.filename} (+{f.additions}/-{f.deletions})\n{(f.patch or '')[:MAX_PATCH_CHARS]}" for f in files]
    return "\n".join(parts)[:MAX_DIFF_CHARS]

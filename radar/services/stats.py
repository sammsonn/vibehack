"""The number on the live repo: fetches review history from GitHub and Radar's decisions from SQLite."""
import datetime as dt

from github.IssueEvent import IssueEvent
from github.PaginatedList import PaginatedList
from github.Repository import Repository

from radar.config import Settings
from radar.core.metrics import ReviewRecord, buy_line, compute, render, savings
from radar.integrations import github
from radar.storage import store


def _is_bot(user) -> bool:
    return user is None or user.type == "Bot" or user.login.endswith("[bot]")


def fetch(repo: Repository, since: dt.datetime) -> list[ReviewRecord]:
    """PRs created since `since` with their first non-author, non-bot review."""
    records = []
    for p in repo.get_pulls(state="all", sort="created", direction="desc"):
        if p.created_at < since:
            break
        if p.draft:
            continue
        start = p.created_at
        for e in PaginatedList(IssueEvent, p._requester, f"{p.issue_url}/events", None):
            if e.event == "ready_for_review":
                start = e.created_at
        reviews = [
            r for r in p.get_reviews()
            if r.submitted_at and r.state != "PENDING" and not _is_bot(r.user)
            and r.user.login.lower() != p.user.login.lower() and r.submitted_at >= start
        ]
        first = min(reviews, key=lambda r: r.submitted_at, default=None)
        records.append(ReviewRecord(
            number=p.number,
            title=p.title,
            author=p.user.login,
            start=start,
            first_review_at=first.submitted_at if first else None,
            first_reviewer=first.user.login if first else None,
            open=p.state == "open",
        ))
    return records


def report(s: Settings, days: int = 30, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    store.init()
    repo = github.live_repo(s)
    since = now - dt.timedelta(days=days)
    st = compute(fetch(repo, since), store.all_suggestions(), now)
    usage = store.llm_usage_since(since)
    sv = savings(
        st,
        fallback_baseline_hours=s.baseline_wait_hours,
        blocked_cost_factor=s.blocked_cost_factor,
        hourly_eur=s.dev_hourly_eur,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        usd_per_m_input=s.llm_usd_per_m_input,
        usd_per_m_output=s.llm_usd_per_m_output,
    )
    buy = buy_line(s.buy_tool_name, s.buy_tool_usd_per_seat, len(s.team.members), sv.model_usd, days,
                   s.hosting_usd_per_month)
    return render(st, repo.full_name, days, sv, buy)

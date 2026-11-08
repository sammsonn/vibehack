"""The number on the live repo: fetches review history from GitHub and Radar's decisions from SQLite."""
import datetime as dt

from github.IssueEvent import IssueEvent
from github.PaginatedList import PaginatedList
from github.Repository import Repository

from radar.config import Settings
from radar.core.metrics import ReviewRecord, Savings, Stats, buy_line, compute, render, savings
from radar.integrations import github, google_calendar
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


def number(s: Settings, days: int = 30, now: dt.datetime | None = None,
           since: dt.datetime | None = None, until: dt.datetime | None = None) -> tuple[Stats, Savings, str]:
    """(stats, savings, repo full name) for the last `days`, or for an explicit since/until window (e.g. one
    pilot phase). PRs opened after `until` are left out."""
    now = now or dt.datetime.now(dt.timezone.utc)
    store.init()
    repo = github.live_repo(s)
    window = since is not None
    until = until or now
    since = since or now - dt.timedelta(days=days)
    records = [r for r in fetch(repo, since) if r.start <= until]
    rows = store.all_suggestions()
    if window:
        rows = [r for r in rows if since <= dt.datetime.fromisoformat(r["suggested_at"]) <= until]
    st = compute(records, rows, until)
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
    return st, sv, repo.full_name


def report(s: Settings, days: int = 30, now: dt.datetime | None = None,
           since: dt.datetime | None = None, until: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    st, sv, repo_name = number(s, days, now, since, until)
    period, window_days = None, days
    if since is not None:
        until = until or now
        tz = google_calendar.timezone_of(s.team.lead.email)
        a, b = since.astimezone(tz), until.astimezone(tz)
        period = f"{a:%d %b %H:%M}–{b:%H:%M}" if a.date() == b.date() else f"{a:%d %b %H:%M}–{b:%d %b %H:%M}"
        window_days = max((until - since).total_seconds() / 86400, 1 / 24)
    buy = buy_line(s.buy_tool_name, s.buy_tool_usd_per_seat, len(s.team.members), sv.model_usd, window_days,
                   s.hosting_usd_per_month)
    return render(st, repo_name, days, sv, buy, period=period)

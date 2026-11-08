"""Collects the App Home snapshot from GitHub, Calendar and SQLite. Short caches keep tab opens fast and
spare the GitHub rate limit when the whole team opens the tab at once."""
import datetime as dt
import logging
import threading
import time

from radar.config import Settings
from radar.core import dashboard
from radar.core.dashboard import OpenPR, Snapshot
from radar.core.metrics import buy_line
from radar.integrations import github, google_calendar
from radar.services import approval_gate, stats, sweep
from radar.storage import store

log = logging.getLogger(__name__)
SNAPSHOT_SECONDS = 60
STATS_SECONDS = 600  # the number walks every PR's history: expensive
STATS_DAYS = 7
HANDOFF_DAYS = 7
CALENDAR_HOURS = 12
_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, seconds: float, fn, force: bool = False):
    with _lock:
        hit = _cache.get(key)
    if hit and not force and time.monotonic() - hit[0] < seconds:
        return hit[1]
    value = fn()
    with _lock:
        _cache[key] = (time.monotonic(), value)
    return value


def _number(s: Settings, now: dt.datetime):
    try:
        st, sv, _ = stats.number(s, STATS_DAYS, now)
        buy = buy_line(s.buy_tool_name, s.buy_tool_usd_per_seat, len(s.team.members), sv.model_usd, STATS_DAYS,
                       s.hosting_usd_per_month)
        return st, sv, buy
    except Exception:
        log.exception("dashboard stats failed")
        return None, None, None


def _gates(repo, pulls) -> dict[int, str]:
    gates = {}
    for p in pulls:
        if p.draft:
            continue
        try:
            state = github.commit_status(repo, p.head.sha, approval_gate.CONTEXT)
        except Exception:
            log.warning("merge gate status for #%s failed", p.number, exc_info=True)
            continue
        if state:
            gates[p.number] = state
    return gates


def collect(s: Settings, now: dt.datetime) -> Snapshot:
    store.init()
    lead = s.team.lead
    repo = github.live_repo(s)
    pulls = github.open_prs(repo)
    prs = [
        OpenPR(p.number, p.title, p.html_url, p.user.login, p.draft, p.created_at, github.request_times(p))
        for p in pulls
    ]
    horizon = now + dt.timedelta(hours=CALENDAR_HOURS)
    busy = google_calendar.busy_blocks([m.email for m in s.team.members], now, horizon)
    rows = store.all_suggestions()
    st, sv, buy = _cached("stats", STATS_SECONDS, lambda: _number(s, now))
    return Snapshot(
        at=now,
        horizon=horizon,
        prs=prs,
        busy=busy,
        handoffs=dashboard.handoffs(rows, prs, now - dt.timedelta(days=HANDOFF_DAYS)),
        stats=st,
        savings=sv,
        stats_days=STATS_DAYS,
        buy=buy,
        pending=dashboard.pending(rows),
        snoozed=dashboard.snoozed(rows, now),
        triage=dashboard.latest_triage(rows),
        review_blocks=google_calendar.upcoming_review_blocks(lead.email, now),
        lead_status=sweep.lead_context(s, now, busy[lead.email]).header,
        gates=_gates(repo, pulls),
    )


def snapshot(s: Settings, force: bool = False) -> Snapshot:
    return _cached("snapshot", SNAPSHOT_SECONDS, lambda: collect(s, dt.datetime.now(dt.timezone.utc)), force)

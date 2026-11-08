"""Collects the App Home snapshot from GitHub, Calendar and SQLite. Short caches keep tab opens fast and
spare the GitHub rate limit when the whole team opens the tab at once."""
import datetime as dt
import logging
import threading
import time

from radar.config import Settings
from radar.core import dashboard
from radar.core.dashboard import OpenPR, Snapshot
from radar.integrations import github, google_calendar
from radar.services import stats
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
        return st, sv
    except Exception:
        log.exception("dashboard stats failed")
        return None, None


def collect(s: Settings, now: dt.datetime) -> Snapshot:
    store.init()
    repo = github.live_repo(s)
    prs = [
        OpenPR(p.number, p.title, p.html_url, p.user.login, p.draft, p.created_at, github.request_times(p))
        for p in github.open_prs(repo)
    ]
    horizon = now + dt.timedelta(hours=CALENDAR_HOURS)
    busy = google_calendar.busy_blocks([m.email for m in s.team.members], now, horizon)
    handoffs = dashboard.handoffs(store.all_suggestions(), prs, now - dt.timedelta(days=HANDOFF_DAYS))
    st, sv = _cached("stats", STATS_SECONDS, lambda: _number(s, now))
    return Snapshot(now, horizon, prs, busy, handoffs, st, sv, STATS_DAYS)


def snapshot(s: Settings, force: bool = False) -> Snapshot:
    return _cached("snapshot", SNAPSHOT_SECONDS, lambda: collect(s, dt.datetime.now(dt.timezone.utc)), force)

"""'While you were in calls' digest: once the lead's busy stretch ends, what Radar's handoffs and review blocks led to."""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from radar.config import Settings
from radar.core import schedule
from radar.core.models import Suggestion
from radar.integrations import github, google_calendar
from radar.storage import store

log = logging.getLogger(__name__)
LOOKBACK = dt.timedelta(hours=12)
MAX_AGE = dt.timedelta(minutes=30)  # skip stretches that ended long ago, e.g. right after a restart
SLACK = dt.timedelta(minutes=1)


@dataclass(frozen=True)
class Line:
    pr_number: int
    title: str
    url: str
    icon: str
    text: str


def last_stretch(blocks: list[schedule.Block], now: dt.datetime) -> schedule.Block | None:
    """The most recent busy stretch that has already ended."""
    ended = [b for b in schedule.merge(blocks) if b[1] <= now]
    return max(ended, key=lambda b: b[1], default=None)


def acted_during(rows, start: dt.datetime, end: dt.datetime) -> list[tuple[object, Suggestion]]:
    """Handoffs and review blocks the lead approved on cards made for the busy stretch [start, end]."""
    result = []
    for row in rows:
        if row["status"] not in ("handed_off", "blocked"):
            continue
        sg = Suggestion.from_json(row["payload"])
        if sg.lead_busy_until and start < sg.lead_busy_until <= end + SLACK:
            result.append((row, sg))
    return result


def latest_review(reviews, login: str, since: dt.datetime):
    """Latest submitted review by `login` after `since`, or None."""
    mine = [r for r in reviews if r.user and r.user.login.lower() == login.lower()
            and r.submitted_at and r.submitted_at >= since and r.state != "PENDING"]
    return max(mine, key=lambda r: r.submitted_at, default=None)


def review_outcome(reviews, login: str, since: dt.datetime) -> str | None:
    review = latest_review(reviews, login, since)
    return review.state if review else None


def review_key(pr_number: int, review_id: int) -> str:
    """Marks a review as reported to the lead, by the digest or by an instant alert."""
    return f"review:{pr_number}:{review_id}"


OUTCOMES = {
    "APPROVED": ("✅", "approved"),
    "CHANGES_REQUESTED": ("✍️", "requested changes on"),
    "COMMENTED": ("💬", "commented on"),
    "DISMISSED": ("💬", "reviewed"),
}


def handoff_line(name: str, outcome: str | None, merged: bool, closed: bool) -> tuple[str, str]:
    if merged:
        return "🚀", f"{name}'s review is done, PR merged"
    if outcome:
        icon, verb = OUTCOMES.get(outcome, ("💬", "reviewed"))
        return icon, f"{name} {verb} it"
    if closed:
        return "🗑️", "closed before review"
    return "👀", f"{name} is on it (no review yet)"


def due(s: Settings, now: dt.datetime) -> tuple[schedule.Block, list[Line]] | None:
    """The digest to send now, at most once per busy stretch. Call only while the lead is free."""
    lead = s.team.lead
    blocks = google_calendar.busy_blocks([lead.email], now - LOOKBACK, now)[lead.email]
    stretch = last_stretch(blocks, now)
    if stretch is None or now - stretch[1] > MAX_AGE:
        return None
    acted = acted_during(store.all_suggestions(), *stretch)
    if not acted or not store.fire_once(f"digest:{stretch[1].isoformat()}"):
        return None
    repo = github.live_repo(s)
    lines = []
    for row, sg in acted:
        if row["status"] == "blocked":
            lines.append(Line(sg.pr.number, sg.pr.title, sg.pr.url, "📅", row["result"] or "review block booked"))
            continue
        login = row["handed_to"] or (sg.reviewer.github if sg.reviewer else "")
        pr = repo.get_pull(sg.pr.number)
        since = dt.datetime.fromisoformat(row["decided_at"])
        review = latest_review(pr.get_reviews(), login, since)
        if review:
            store.fire_once(review_key(sg.pr.number, review.id))
        icon, text = handoff_line(s.team.display_name(login), review.state if review else None,
                                  pr.merged, pr.state == "closed")
        lines.append(Line(sg.pr.number, sg.pr.title, sg.pr.url, icon, text))
    log.info("digest for busy stretch ending %s: %d PRs", stretch[1].isoformat(), len(lines))
    return stretch, lines

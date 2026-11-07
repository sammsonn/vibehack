"""Tells the lead, once per review, what happened to a PR he handed off. Reviews that land while he is in calls
are left to the post-call digest, which marks them as announced."""
from __future__ import annotations

import datetime as dt
import logging

from radar.config import Settings
from radar.core import schedule
from radar.core.models import Suggestion
from radar.integrations import google_calendar
from radar.services import digest
from radar.storage import store

log = logging.getLogger(__name__)


def unannounced(rows, reviews) -> list[tuple[object, Suggestion, str, object]]:
    """(row, suggestion, reviewer login, review) for each handed-off reviewer's latest review not yet announced."""
    result, seen = [], set()
    for row in rows:
        sg = Suggestion.from_json(row["payload"])
        login = (row["handed_to"] or (sg.reviewer.github if sg.reviewer else "")).lower()
        if not login or login in seen:
            continue
        seen.add(login)
        review = digest.latest_review(reviews, login, dt.datetime.fromisoformat(row["decided_at"]))
        if review and not store.was_fired(digest.review_key(sg.pr.number, review.id)):
            result.append((row, sg, login, review))
    return result


def announce_now(review_at: dt.datetime, in_digest: bool, busy_now: bool,
                 stretch: schedule.Block | None, now: dt.datetime) -> bool:
    if busy_now or now - review_at > digest.LOOKBACK:
        return False
    digest_pending = stretch is not None and now - stretch[1] <= digest.MAX_AGE
    return not (in_digest and digest_pending and review_at <= stretch[1] + digest.SLACK)


def check(s: Settings, pr, reviews, notify, now: dt.datetime | None = None) -> None:
    """pr: PyGithub PullRequest. notify(sg, login, state, unblocked) sends the DM."""
    items = unannounced(store.handed_off(pr.number), reviews)
    if not items:
        return
    now = now or dt.datetime.now(dt.timezone.utc)
    lead = s.team.lead.email
    blocks = google_calendar.busy_blocks([lead], now - digest.LOOKBACK, now + dt.timedelta(minutes=1))[lead]
    busy_now = schedule.busy_until(blocks, now) is not None
    stretch = digest.last_stretch(blocks, now)
    for row, sg, login, review in items:
        in_digest = stretch is not None and bool(digest.acted_during([row], *stretch))
        if not announce_now(review.submitted_at, in_digest, busy_now, stretch, now):
            continue
        if store.fire_once(digest.review_key(sg.pr.number, review.id)):
            unblocked = review.state == "APPROVED" and review.commit_id == pr.head.sha
            log.info("review alert: %s %s #%s", login, review.state, sg.pr.number)
            notify(sg, login, review.state, unblocked)

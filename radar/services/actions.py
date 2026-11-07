"""Executes a suggestion after the lead clicks. Every GitHub/Calendar write in Radar goes through here."""
import datetime as dt
import logging

from radar.config import Settings
from radar.core import schedule
from radar.core.models import Candidate, Suggestion
from radar.integrations import github, google_calendar
from radar.services import approval_gate
from radar.storage import store

log = logging.getLogger(__name__)
SNOOZE_HOURS = 2


def _handoff_comment(s: Settings, sg: Suggestion, reviewer: Candidate, tz: dt.tzinfo) -> str:
    lead = s.team.lead
    busy = f"is busy until {sg.lead_busy_until.astimezone(tz):%H:%M}" if sg.lead_busy_until else "is busy"
    return (
        f"🛰️ **Review Radar**: @{lead.github} {busy} and handed this review to @{reviewer.github}.\n\n"
        f"**Summary:** {sg.triage.summary}\n"
        f"**Risk:** {sg.triage.risk} ({sg.triage.risk_reason})\n"
        f"**Look first:** `{sg.triage.look_first}`\n\n"
        f"_Approved by {lead.name} in Slack._"
    )


def _block_comment(s: Settings, sg: Suggestion, start: dt.datetime, end: dt.datetime, tz: dt.tzinfo) -> str:
    start, end = start.astimezone(tz), end.astimezone(tz)
    return (
        f"🛰️ **Review Radar**: @{s.team.lead.github} reserved **{start:%H:%M}–{end:%H:%M}** ({start:%a %d %b}) "
        f"to review this.\n\n"
        f"**Risk:** {sg.triage.risk} ({sg.triage.risk_reason})\n"
        f"**Look first:** `{sg.triage.look_first}`"
    )


def _approve(s: Settings, sid: int, sg: Suggestion, chosen: Candidate | None = None, on_handoff=None) -> None:
    """chosen: a reviewer the lead picked from the ranking; overrides Radar's suggestion.
    on_handoff(sg, reviewer_github): called after a successful handoff, e.g. to DM the reviewer."""
    lead = s.team.lead
    tz = google_calendar.timezone_of(lead.email)
    repo = github.live_repo(s)
    pr = github.still_waiting_on(repo, sg.pr.number, lead.github)
    if pr is None:
        store.finish(sid, "stale", f"#{sg.pr.number} is closed or no longer waiting on {lead.name}")
        return
    reviewer = chosen or (sg.reviewer if sg.action == "handoff" else None)
    if reviewer:
        github.hand_off(pr, lead.github, reviewer.github, _handoff_comment(s, sg, reviewer, tz))
        own_pick = chosen is not None and (sg.reviewer is None or chosen.github != sg.reviewer.github)
        store.finish(sid, "handed_off", f"Handed off to {reviewer.name}{' (your pick)' if own_pick else ''}",
                     handed_to=reviewer.github)
        try:
            approval_gate.sync(s, [sg.pr.number])
        except Exception:  # the gate loop retries within a minute
            log.warning("approval gate update for #%s failed", sg.pr.number, exc_info=True)
        if on_handoff:
            try:
                on_handoff(sg, reviewer.github)
            except Exception:  # the handoff itself already succeeded
                log.warning("could not notify %s about #%s", reviewer.github, sg.pr.number, exc_info=True)
    elif sg.slot:
        start, end = sg.slot
        now = dt.datetime.now(dt.timezone.utc)
        existing = google_calendar.upcoming_review_blocks(lead.email, now).get(sg.pr.number)
        if existing:  # e.g. a button on an older card: never book the same PR twice
            store.finish(sid, "blocked", f"Review block already booked {schedule.describe_slot(existing, tz, now)}")
            return
        if start < now:  # approved late (e.g. after a reminder): take the next free slot instead
            blocks = google_calendar.busy_blocks([lead.email], now, now + dt.timedelta(days=3))[lead.email]
            slot = schedule.first_free_slot(blocks, now, int((end - start).total_seconds() // 60), tz)
            if slot is None:
                store.finish(sid, "failed", "no free slot left in working hours")
                return
            start, end = slot
        description = f"{sg.pr.url}\n\n{sg.triage.summary}\n\nRisk: {sg.triage.risk} ({sg.triage.risk_reason})\nLook first: {sg.triage.look_first}"
        link = google_calendar.create_review_block(
            lead.email, start, end, f"{google_calendar.REVIEW_BLOCK_PREFIX} #{sg.pr.number}: {sg.pr.title}", description,
            sg.pr.number,
        )
        pr.create_issue_comment(_block_comment(s, sg, start, end, tz))
        label = f"Review block {schedule.describe_slot((start, end), tz, now)} added"
        store.finish(sid, "blocked", f"<{link}|{label}>" if link else label)
    else:
        store.finish(sid, "failed", "nothing to approve")


def execute(s: Settings, sid: int, verb: str, user: str, on_claimed=None, reviewer: str | None = None,
            on_handoff=None) -> bool:
    """verb: approve | pick (reviewer = GitHub login from the ranking) | keep | snooze.
    Returns False if the suggestion was already decided or the pick is not an eligible reviewer."""
    row = store.suggestion(sid)
    if row is None:
        return False
    chosen = Suggestion.from_json(row["payload"]).candidate(reviewer) if verb == "pick" and reviewer else None
    if verb == "pick" and chosen is None:
        return False
    if not store.claim(sid, user):
        return False
    if on_claimed:
        on_claimed()
    sg = Suggestion.from_json(row["payload"])
    try:
        if verb == "approve":
            _approve(s, sid, sg, on_handoff=on_handoff)
        elif verb == "pick":
            _approve(s, sid, sg, chosen, on_handoff=on_handoff)
        elif verb == "keep":
            store.finish(sid, "kept")
        elif verb == "snooze":
            until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=SNOOZE_HOURS)
            store.finish(sid, "snoozed", snoozed_until=until)
        else:
            store.finish(sid, "failed", f"unknown action {verb}")
    except Exception as e:
        log.exception("action %s on suggestion %s failed", verb, sid)
        store.finish(sid, "failed", str(e)[:200])
    return True


def approve_all(s: Settings, card_id: int, user: str, on_claimed=None, on_handoff=None) -> None:
    for row in store.card_suggestions(card_id):
        sg = Suggestion.from_json(row["payload"])
        if row["status"] == "pending" and (sg.action == "handoff" or sg.slot):
            execute(s, row["id"], "approve", user, on_claimed, on_handoff=on_handoff)

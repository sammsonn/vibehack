"""Executes a suggestion after the lead clicks. Every GitHub/Calendar write in Radar goes through here."""
import datetime as dt
import logging

from radar import approval_gate, calendar_api, github_api, store
from radar.config import Settings
from radar.models import Suggestion
from radar.slack_ui import SNOOZE_HOURS

log = logging.getLogger(__name__)


def _handoff_comment(s: Settings, sg: Suggestion, tz: dt.tzinfo) -> str:
    lead = s.team.lead
    busy = f"is in calls until {sg.lead_busy_until.astimezone(tz):%H:%M}" if sg.lead_busy_until else "is busy"
    return (
        f"🛰️ **Review Radar**: @{lead.github} {busy} and handed this review to @{sg.reviewer.github}.\n\n"
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


def _approve(s: Settings, sid: int, sg: Suggestion) -> None:
    lead = s.team.lead
    tz = calendar_api.timezone_of(lead.email)
    repo = github_api.live_repo(s)
    pr = github_api.still_waiting_on(repo, sg.pr.number, lead.github)
    if pr is None:
        store.finish(sid, "stale", f"#{sg.pr.number} is closed or no longer waiting on {lead.name}")
        return
    if sg.action == "handoff":
        github_api.hand_off(pr, lead.github, sg.reviewer.github, _handoff_comment(s, sg, tz))
        store.finish(sid, "handed_off", f"Handed off to {sg.reviewer.name}")
        try:
            approval_gate.sync(s, [sg.pr.number])
        except Exception:  # the gate loop retries within a minute
            log.warning("approval gate update for #%s failed", sg.pr.number, exc_info=True)
    elif sg.slot:
        start, end = sg.slot
        now = dt.datetime.now(dt.timezone.utc)
        if start < now:  # approved late (e.g. after a reminder): take the next free slot instead
            blocks = calendar_api.busy_blocks([lead.email], now, now + dt.timedelta(days=3))[lead.email]
            slot = calendar_api.first_free_slot(blocks, now, int((end - start).total_seconds() // 60), tz)
            if slot is None:
                store.finish(sid, "failed", "no free slot left in working hours")
                return
            start, end = slot
        description = f"{sg.pr.url}\n\n{sg.triage.summary}\n\nRisk: {sg.triage.risk} ({sg.triage.risk_reason})\nLook first: {sg.triage.look_first}"
        link = calendar_api.create_event(lead.email, start, end, f"🔍 Review PR #{sg.pr.number}: {sg.pr.title}", description)
        pr.create_issue_comment(_block_comment(s, sg, start, end, tz))
        label = f"Review block {start.astimezone(tz):%H:%M}–{end.astimezone(tz):%H:%M} added"
        store.finish(sid, "blocked", f"<{link}|{label}>" if link else label)
    else:
        store.finish(sid, "failed", "nothing to approve")


def execute(s: Settings, sid: int, verb: str, user: str, on_claimed=None) -> bool:
    """verb: approve | keep | snooze. Returns False if the suggestion was already decided."""
    row = store.suggestion(sid)
    if row is None or not store.claim(sid, user):
        return False
    if on_claimed:
        on_claimed()
    sg = Suggestion.from_json(row["payload"])
    try:
        if verb == "approve":
            _approve(s, sid, sg)
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


def approve_all(s: Settings, card_id: int, user: str, on_claimed=None) -> None:
    for row in store.card_suggestions(card_id):
        sg = Suggestion.from_json(row["payload"])
        if row["status"] == "pending" and (sg.action == "handoff" or sg.slot):
            execute(s, row["id"], "approve", user, on_claimed)

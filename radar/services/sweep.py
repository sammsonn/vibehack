"""Builds Radar suggestions from live GitHub + Calendar data and decides when to sweep automatically."""
import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from radar.config import Settings
from radar.core import ranking, schedule
from radar.core.models import Suggestion
from radar.integrations import github, google_calendar
from radar.services import reminders, triage
from radar.storage import store

log = logging.getLogger(__name__)
BLOCK_MINUTES = 30
LLM_WORKERS = 2  # Featherless counts concurrent requests per plan


@dataclass(frozen=True)
class LeadContext:
    header: str
    busy_until: dt.datetime | None
    upcoming_event: dict | None


def fmt_time(t: dt.datetime, tz: dt.tzinfo) -> str:
    return t.astimezone(tz).strftime("%H:%M")


def lead_context(s: Settings, now: dt.datetime, lead_blocks: list[schedule.Block]) -> LeadContext:
    lead = s.team.lead.email
    tz = google_calendar.timezone_of(lead)
    window = dt.timedelta(minutes=s.precall_minutes)
    events = google_calendar.events_between(lead, now, now + window)
    current = [e for e in events if e["start"] <= now < e["end"]]
    current_call = next((e for e in current if not e["review_block"]), None)
    upcoming = next((e for e in events if now < e["start"] <= now + window and not e["review_block"]), None)
    if current_call:
        until = schedule.busy_until(lead_blocks, now) or current_call["end"]
        return LeadContext(f"📅 You're in *{current_call['summary']}* until {fmt_time(until, tz)}", until, None)
    if current:
        until = schedule.busy_until(lead_blocks, now) or current[0]["end"]
        return LeadContext(f"🔍 You're in a review block until {fmt_time(until, tz)}", until, None)
    if upcoming:
        until = schedule.busy_until(lead_blocks, upcoming["start"]) or upcoming["end"]
        minutes = max(1, round((upcoming["start"] - now).total_seconds() / 60))
        return LeadContext(
            f"📅 *{upcoming['summary']}* starts in {minutes} min (busy until {fmt_time(until, tz)})", until, upcoming
        )
    return LeadContext("No calls right now", None, None)


def build(s: Settings, now: dt.datetime, auto: bool = False,
          min_wait: dt.timedelta | None = None) -> tuple[LeadContext, list[Suggestion]]:
    """auto=True skips PRs already on an open card, decided for this commit, or snoozed."""
    team = s.team
    lead = team.lead
    repo = github.live_repo(s)
    pulls = github.open_prs(repo)
    prs = github.waiting_on(pulls, lead.github)
    if min_wait:
        prs = [p for p in prs if now - p.waiting_since >= min_wait]
    if auto:
        prs = [p for p in prs if not store.recently_handled(p.number, p.head_sha, now)]
    prs.sort(key=lambda p: p.waiting_since)  # longest-waiting PR gets the earliest review slot

    horizon = now + dt.timedelta(days=3)
    busy = google_calendar.busy_blocks([m.email for m in team.members], now, horizon)
    ctx = lead_context(s, now, busy[lead.email])
    if not prs:
        return ctx, []

    with ThreadPoolExecutor(max_workers=LLM_WORKERS) as pool:
        triages = list(pool.map(lambda p: triage.triage(s, p), prs))

    open_reviews = github.open_review_counts(pulls)
    tz = google_calendar.timezone_of(lead.email)
    lead_blocks = list(busy[lead.email])
    suggestions = []
    for pr, risk in zip(prs, triages):
        ranked = ranking.rank(pr, team, github.file_experts(repo, pr.files), open_reviews, busy, now)
        slot = schedule.first_free_slot(lead_blocks, ctx.busy_until or now, BLOCK_MINUTES, tz)
        sg = ranking.decide(pr, risk, ranked, slot, ctx.busy_until)
        if sg.action == "block" and sg.slot:
            lead_blocks.append(sg.slot)  # next high-risk PR gets the following free slot
        elif sg.action == "handoff":
            open_reviews[sg.reviewer.github.lower()] += 1
        suggestions.append(sg)
    # High risk first: those are the ones the lead must look at.
    suggestions.sort(key=lambda sg: (sg.triage.risk != "high", sg.pr.waiting_since))
    return ctx, suggestions


def run(s: Settings, post, now: dt.datetime | None = None, trigger: str = "manual",
        auto: bool = False, min_wait: dt.timedelta | None = None) -> int | None:
    """Builds suggestions and, if there are any, stores a card and calls post(card_id). Returns the card id."""
    now = now or dt.datetime.now(dt.timezone.utc)
    ctx, suggestions = build(s, now, auto=auto, min_wait=min_wait)
    log.info("sweep (%s) at %s: %d suggestions", trigger, now.isoformat(), len(suggestions))
    if not suggestions:
        return None
    card_id = store.create_card(trigger, ctx.header, suggestions)
    post(card_id)
    return card_id


def tick(s: Settings, post, now: dt.datetime | None = None, remind=None) -> int | None:
    """Automatic check: a sweep right before each of the lead's calls, stale PRs while the lead is busy, and
    once the lead is free again, one reminder per card that is still unclicked (remind(card_id, suggestions))."""
    now = now or dt.datetime.now(dt.timezone.utc)
    lead = s.team.lead.email
    lead_blocks = google_calendar.busy_blocks([lead], now, now + dt.timedelta(minutes=1))[lead]
    busy_now = schedule.busy_until(lead_blocks, now) is not None
    if remind and not busy_now:
        for card_id, pending in reminders.due(s, now):
            remind(card_id, pending)
    window = dt.timedelta(minutes=s.precall_minutes)
    upcoming = [e for e in google_calendar.events_between(lead, now, now + window)
                if now < e["start"] <= now + window and not e["review_block"]]
    if upcoming and store.fire_once(f"precall:{upcoming[0]['id']}:{upcoming[0]['start'].isoformat()}"):
        return run(s, post, now, trigger="precall", auto=True)
    if busy_now:
        return run(s, post, now, trigger="stale", auto=True, min_wait=dt.timedelta(hours=s.stale_hours))
    return None

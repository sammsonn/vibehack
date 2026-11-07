"""Builds Radar suggestions from live GitHub + Calendar data and decides when to sweep automatically."""
import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from radar import calendar_api, github_api, llm, ranking, reminders, store
from radar.config import Settings
from radar.models import Suggestion

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


def lead_context(s: Settings, now: dt.datetime, lead_blocks: list[calendar_api.Block]) -> LeadContext:
    lead = s.team.lead.email
    tz = calendar_api.timezone_of(lead)
    window = dt.timedelta(minutes=s.precall_minutes)
    events = calendar_api.events_between(lead, now, now + window)
    current = next((e for e in events if e["start"] <= now < e["end"]), None)
    upcoming = next((e for e in events if now < e["start"] <= now + window), None)
    if current:
        until = calendar_api.busy_until(lead_blocks, now) or current["end"]
        return LeadContext(f"📅 You're in *{current['summary']}* until {fmt_time(until, tz)}", until, None)
    if upcoming:
        until = calendar_api.busy_until(lead_blocks, upcoming["start"]) or upcoming["end"]
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
    repo = github_api.live_repo(s)
    pulls = github_api.open_prs(repo)
    prs = github_api.waiting_on(pulls, lead.github)
    if min_wait:
        prs = [p for p in prs if now - p.waiting_since >= min_wait]
    if auto:
        prs = [p for p in prs if not store.recently_handled(p.number, p.head_sha, now)]

    horizon = now + dt.timedelta(days=3)
    busy = calendar_api.busy_blocks([m.email for m in team.members], now, horizon)
    ctx = lead_context(s, now, busy[lead.email])
    if not prs:
        return ctx, []

    with ThreadPoolExecutor(max_workers=LLM_WORKERS) as pool:
        triages = list(pool.map(lambda p: llm.triage(s, p), prs))

    open_reviews = github_api.open_review_counts(pulls)
    tz = calendar_api.timezone_of(lead.email)
    lead_blocks = list(busy[lead.email])
    suggestions = []
    for pr, triage in zip(prs, triages):
        ranked = ranking.rank(pr, team, github_api.file_experts(repo, pr.files), open_reviews, busy, now)
        slot = calendar_api.first_free_slot(lead_blocks, ctx.busy_until or now, BLOCK_MINUTES, tz)
        sg = ranking.decide(pr, triage, ranked, slot, ctx.busy_until)
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
    lead_blocks = calendar_api.busy_blocks([lead], now, now + dt.timedelta(minutes=1))[lead]
    busy_now = calendar_api.busy_until(lead_blocks, now) is not None
    if remind and not busy_now:
        for card_id, pending in reminders.due(s, now):
            remind(card_id, pending)
    window = dt.timedelta(minutes=s.precall_minutes)
    upcoming = [e for e in calendar_api.events_between(lead, now, now + window) if now < e["start"] <= now + window]
    if upcoming and store.fire_once(f"precall:{upcoming[0]['id']}:{upcoming[0]['start'].isoformat()}"):
        return run(s, post, now, trigger="precall", auto=True)
    if busy_now:
        return run(s, post, now, trigger="stale", auto=True, min_wait=dt.timedelta(hours=s.stale_hours))
    return None

"""Posts and updates Radar's Slack messages. Cards are re-rendered from SQLite after every click."""
import datetime as dt
import logging
from functools import cache

from slack_sdk import WebClient

from radar.config import Settings
from radar.core import schedule
from radar.core.models import Suggestion
from radar.integrations import google_calendar
from radar.services.digest import OUTCOMES, Line
from radar.slack.blocks import duration, render
from radar.storage import store

log = logging.getLogger(__name__)

RISK_ICON = {"low": "🟢", "high": "🔴"}


@cache
def slack_id(client: WebClient, email: str) -> str:
    return client.users_lookupByEmail(email=email)["user"]["id"]


def _dm(client: WebClient, email: str, text: str) -> None:
    channel = client.conversations_open(users=slack_id(client, email))["channel"]["id"]
    client.chat_postMessage(channel=channel, text=text, unfurl_links=False, unfurl_media=False)


def handoff_text(s: Settings, sg: Suggestion, tz: dt.tzinfo) -> str:
    lead = s.team.lead.name
    busy = f" (busy until {sg.lead_busy_until.astimezone(tz):%H:%M})" if sg.lead_busy_until else ""
    tr = sg.triage
    return (
        f"🛰️ *{lead} handed you a review*{busy}\n"
        f"*<{sg.pr.url}|#{sg.pr.number} {sg.pr.title}>* by {s.team.display_name(sg.pr.author)}\n"
        f"{RISK_ICON.get(tr.risk, '')} {tr.risk.capitalize()} risk · look first: `{tr.look_first}`\n"
        f"> {tr.summary}\n"
        f"Your approval unblocks the merge: <{sg.pr.url}/files|Review it on GitHub →>"
    )


def notify_reviewer(client: WebClient, s: Settings, sg: Suggestion, reviewer_github: str) -> None:
    member = s.team.by_github(reviewer_github)
    if member:
        _dm(client, member.email, handoff_text(s, sg, google_calendar.timezone_of(s.team.lead.email)))


def digest_text(stretch: schedule.Block, lines: list[Line], tz: dt.tzinfo) -> str:
    start, end = (t.astimezone(tz) for t in stretch)
    body = "\n".join(f"{ln.icon} <{ln.url}|#{ln.pr_number} {ln.title}>: {ln.text}" for ln in lines)
    return f"☕ *You're out of calls.* While you were busy ({start:%H:%M}–{end:%H:%M}), Radar's handoffs did this:\n{body}"


def post_digest(client: WebClient, s: Settings, stretch: schedule.Block, lines: list[Line]) -> None:
    lead = s.team.lead.email
    _dm(client, lead, digest_text(stretch, lines, google_calendar.timezone_of(lead)))


def review_alert_text(s: Settings, sg: Suggestion, login: str, state: str, unblocked: bool) -> str:
    icon, verb = OUTCOMES.get(state, ("💬", "reviewed"))
    pr = f"<{sg.pr.url}|#{sg.pr.number} {sg.pr.title}>"
    text = f"{icon} *{s.team.display_name(login)} {verb}* {pr}"
    if unblocked:
        return f"{text}: the merge is unblocked."
    if state == "CHANGES_REQUESTED":
        return f"{text}: back with the author."
    return text


def post_review_alert(client: WebClient, s: Settings, sg: Suggestion, login: str, state: str, unblocked: bool) -> None:
    _dm(client, s.team.lead.email, review_alert_text(s, sg, login, state, unblocked))


def post_card(client: WebClient, s: Settings, card_id: int) -> None:
    lead = s.team.lead.email
    tz = google_calendar.timezone_of(lead)
    text, blocks = render(store.card(card_id), store.card_suggestions(card_id), tz, s.team)
    channel = client.conversations_open(users=slack_id(client, lead))["channel"]["id"]
    msg = client.chat_postMessage(channel=channel, text=text, blocks=blocks, unfurl_links=False, unfurl_media=False)
    store.set_card_message(card_id, msg["channel"], msg["ts"])
    # Grey out the rows this card took over, so the same PR never has live buttons on two cards.
    for old_id in store.cards_superseded_by(card_id):
        try:
            refresh_card(client, s, old_id)
        except Exception:  # e.g. the old message was deleted; the new card is what matters
            log.warning("could not update replaced card %s", old_id, exc_info=True)


def refresh_card(client: WebClient, s: Settings, card_id: int) -> None:
    card = store.card(card_id)
    if not card or not card["ts"]:
        return
    text, blocks = render(card, store.card_suggestions(card_id), google_calendar.timezone_of(s.team.lead.email), s.team)
    client.chat_update(channel=card["channel"], ts=card["ts"], text=text, blocks=blocks)


def post_reminder(client: WebClient, s: Settings, card_id: int, pending: list[Suggestion],
                  now: dt.datetime | None = None) -> None:
    """Refreshes the card, then nudges in its thread (also shown in the DM) if anything is still unclicked."""
    refresh_card(client, s, card_id)
    if not pending:
        return
    card = store.card(card_id)
    now = now or dt.datetime.now(dt.timezone.utc)
    oldest = min(pending, key=lambda sg: sg.pr.waiting_since)
    n = len(pending)
    link = client.chat_getPermalink(channel=card["channel"], message_ts=card["ts"])["permalink"]
    client.chat_postMessage(
        channel=card["channel"],
        thread_ts=card["ts"],
        reply_broadcast=True,
        unfurl_links=False,
        unfurl_media=False,
        text=(
            f"⏰ *You're free again.* {n} PR{'s are' if n != 1 else ' is'} still waiting on you "
            f"(longest: #{oldest.pr.number}, {duration(now - oldest.pr.waiting_since)}). "
            f"Nothing has happened yet: <{link}|approve on the card>."
        ),
    )

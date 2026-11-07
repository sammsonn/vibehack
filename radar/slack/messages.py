"""Posts and updates Radar's Slack messages. Cards are re-rendered from SQLite after every click."""
import datetime as dt
from functools import cache

from slack_sdk import WebClient

from radar.config import Settings
from radar.core.models import Suggestion
from radar.integrations import google_calendar
from radar.slack.blocks import duration, render
from radar.storage import store


@cache
def slack_id(client: WebClient, email: str) -> str:
    return client.users_lookupByEmail(email=email)["user"]["id"]


def post_card(client: WebClient, s: Settings, card_id: int) -> None:
    lead = s.team.lead.email
    tz = google_calendar.timezone_of(lead)
    text, blocks = render(store.card(card_id), store.card_suggestions(card_id), tz, s.team)
    channel = client.conversations_open(users=slack_id(client, lead))["channel"]["id"]
    msg = client.chat_postMessage(channel=channel, text=text, blocks=blocks, unfurl_links=False, unfurl_media=False)
    store.set_card_message(card_id, msg["channel"], msg["ts"])


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

"""Slack Block Kit rendering for Radar cards. Cards are re-rendered from SQLite after every click."""
import datetime as dt
from functools import cache

from slack_sdk import WebClient

from radar import calendar_api, store
from radar.config import Settings
from radar.models import Suggestion

SNOOZE_HOURS = 2


@cache
def slack_id(client: WebClient, email: str) -> str:
    return client.users_lookupByEmail(email=email)["user"]["id"]


def _t(value: str | dt.datetime | None, tz: dt.tzinfo) -> str:
    if not value:
        return ""
    t = dt.datetime.fromisoformat(value) if isinstance(value, str) else value
    return t.astimezone(tz).strftime("%H:%M")


def _duration(delta: dt.timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{hours // 24}d {hours % 24}h"


def _button(text: str, action_id: str, value: str, style: str | None = None) -> dict:
    b = {"type": "button", "text": {"type": "plain_text", "text": text, "emoji": True}, "action_id": action_id, "value": value}
    if style:
        b["style"] = style
    return b


def primary_label(sg: Suggestion, tz: dt.tzinfo) -> str | None:
    if sg.action == "handoff":
        return f"✅ Hand off to {sg.reviewer.name}"
    if sg.slot:
        return f"📅 Block {_t(sg.slot[0], tz)}–{_t(sg.slot[1], tz)} to review"
    return None


def _decision_line(sg: Suggestion, tz: dt.tzinfo) -> str:
    if sg.action == "handoff":
        return f"→ Suggest *{sg.reviewer.name}* ({sg.reviewer.why})"
    if sg.slot:
        return f"→ {sg.why}. Next free slot *{_t(sg.slot[0], tz)}–{_t(sg.slot[1], tz)}*"
    return f"→ {sg.why}. No free slot in working hours"


def _outcome(row, tz: dt.tzinfo) -> str:
    who = f"<@{row['decided_by']}>" if row["decided_by"] else ""
    at = _t(row["decided_at"], tz)
    status = row["status"]
    result = row["result"] or ""
    return {
        "working": "⏳ Working on it…",
        "handed_off": f"✅ {result} by {who} at {at}",
        "blocked": f"📅 {result} by {who} at {at}",
        "kept": f"🙋 Kept by {who} at {at}",
        "snoozed": f"⏰ Snoozed by {who} until {_t(row['snoozed_until'], tz)}",
        "stale": f"↩️ Skipped: {result}",
        "failed": f"⚠️ Failed: {result}",
        "superseded": "↪️ Replaced by a newer card",
    }.get(status, status)


def render(card, rows: list, tz: dt.tzinfo) -> tuple[str, list[dict]]:
    created = dt.datetime.fromisoformat(card["created_at"])
    n = len(rows)
    text = f"🛰️ Review Radar: {n} PR{'s' if n != 1 else ''} waiting on you"
    blocks: list[dict] = [{
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*🛰️ Review Radar* · {card['header']}\n{n} PR{'s are' if n != 1 else ' is'} waiting on you."},
    }]
    pending = 0
    for row in rows:
        sg = Suggestion.from_json(row["payload"])
        pr, tr = sg.pr, sg.triage
        emoji = "🔴" if tr.risk == "high" else "🟢"
        look = f" Look first: `{tr.look_first}`" if tr.look_first else ""
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": (
            f"{emoji} *<{pr.url}|#{pr.number} {pr.title}>* (@{pr.author}, {_duration(created - pr.waiting_since)} waiting)\n"
            f"{tr.summary}\n"
            f"*{tr.risk.capitalize()} risk:* {tr.risk_reason.rstrip('.')}.{look}\n"
            f"{_decision_line(sg, tz)}"
        )[:2900]}})
        if row["status"] == "pending":
            pending += 1
            sid = str(row["id"])
            buttons = []
            label = primary_label(sg, tz)
            if label:
                buttons.append(_button(label, f"radar:approve:{sid}", sid, "primary"))
            buttons.append(_button("🙋 Keep", f"radar:keep:{sid}", sid))
            buttons.append(_button(f"⏰ Snooze {SNOOZE_HOURS}h", f"radar:snooze:{sid}", sid))
            blocks.append({"type": "actions", "elements": buttons})
        else:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _outcome(row, tz)}]})
    if pending > 1:
        blocks.append({"type": "divider"})
        blocks.append({"type": "actions", "elements": [
            _button("✅ Approve all suggestions", f"radar:all:{card['id']}", str(card["id"]), "primary"),
        ]})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "Nothing happens without your click."}]})
    return text, blocks


def post_card(client: WebClient, s: Settings, card_id: int) -> None:
    lead = s.team.lead.email
    tz = calendar_api.timezone_of(lead)
    text, blocks = render(store.card(card_id), store.card_suggestions(card_id), tz)
    channel = client.conversations_open(users=slack_id(client, lead))["channel"]["id"]
    msg = client.chat_postMessage(channel=channel, text=text, blocks=blocks)
    store.set_card_message(card_id, msg["channel"], msg["ts"])


def refresh_card(client: WebClient, s: Settings, card_id: int) -> None:
    card = store.card(card_id)
    if not card or not card["ts"]:
        return
    text, blocks = render(card, store.card_suggestions(card_id), calendar_api.timezone_of(s.team.lead.email))
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
        text=(
            f"⏰ *You're free again.* {n} PR{'s are' if n != 1 else ' is'} still waiting on you "
            f"(longest: #{oldest.pr.number}, {_duration(now - oldest.pr.waiting_since)}). "
            f"Nothing has happened yet: <{link}|approve on the card>."
        ),
    )

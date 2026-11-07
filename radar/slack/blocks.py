"""Slack Block Kit rendering for Radar cards. Pure: takes stored rows, returns blocks."""
import datetime as dt

from radar.config import Team
from radar.core.models import Suggestion
from radar.services.actions import SNOOZE_HOURS


def _t(value: str | dt.datetime | None, tz: dt.tzinfo) -> str:
    if not value:
        return ""
    t = dt.datetime.fromisoformat(value) if isinstance(value, str) else value
    return t.astimezone(tz).strftime("%H:%M")


def duration(delta: dt.timedelta) -> str:
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


def _status(c, tz: dt.tzinfo) -> str:
    if c.free:
        return "free"
    return f"busy until {_t(c.busy_until, tz)}" if c.busy_until else "busy soon"


def candidate_label(c, rank: int, suggested: bool, tz: dt.tzinfo) -> str:
    """Short enough for Slack's select menu; the reasons are in ranking_line()."""
    return f"{'⭐ ' if suggested else ''}{rank}. {c.name} · {_status(c, tz)}"[:75]


def ranking_line(sg: Suggestion, tz: dt.tzinfo) -> str | None:
    if not sg.candidates:
        return None
    parts = []
    for i, c in enumerate(sg.candidates, start=1):
        commits = f"{c.file_commits} commit{'s' if c.file_commits != 1 else ''} here"
        reviews = f"{c.open_reviews} open review{'s' if c.open_reviews != 1 else ''}"
        parts.append(f"{i}. *{c.name}*: {_status(c, tz)} · {commits} · {reviews}")
    return "👥 *Ranking*\n" + "\n".join(parts)


def reviewer_picker(sg: Suggestion, sid: str, tz: dt.tzinfo) -> dict | None:
    if not sg.candidates:
        return None
    suggested = sg.reviewer.github if sg.action == "handoff" and sg.reviewer else None
    return {
        "type": "static_select",
        "action_id": f"radar:pick:{sid}",
        "placeholder": {"type": "plain_text", "text": "👥 Choose reviewer…", "emoji": True},
        "options": [
            {"text": {"type": "plain_text", "text": candidate_label(c, i, c.github == suggested, tz), "emoji": True},
             "value": c.github}
            for i, c in enumerate(sg.candidates, start=1)
        ],
        "confirm": {
            "title": {"type": "plain_text", "text": "Hand off this review?"},
            "text": {"type": "mrkdwn", "text": f"Radar will request the review of *#{sg.pr.number}* from the engineer you picked and remove you as reviewer."},
            "confirm": {"type": "plain_text", "text": "Hand off"},
            "deny": {"type": "plain_text", "text": "Cancel"},
        },
    }


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


def render(card, rows: list, tz: dt.tzinfo, team: Team) -> tuple[str, list[dict]]:
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
            f"{emoji} *<{pr.url}|#{pr.number} {pr.title}>* ({team.display_name(pr.author)}, {duration(created - pr.waiting_since)} waiting)\n"
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
            picker = reviewer_picker(sg, sid, tz)
            if picker:
                buttons.append(picker)
                blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": ranking_line(sg, tz)[:2900]}]})
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



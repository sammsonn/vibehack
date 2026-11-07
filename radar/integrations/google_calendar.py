"""Google Calendar access as the bot account. Busy/free math lives in radar.core.schedule."""
from __future__ import annotations

import datetime as dt
import threading
from functools import cache
from zoneinfo import ZoneInfo

import httplib2
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

from radar.core.schedule import Block
from radar.integrations.google_auth import get_credentials

REVIEW_BLOCK_TAG = {"radar": "review-block"}
REVIEW_BLOCK_PREFIX = "🔍 Review PR"
HTTP_TIMEOUT_SECONDS = 30
_local = threading.local()


def _ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def service():
    """One client per thread: the underlying httplib2 connection is not thread-safe, and the scheduler,
    merge gate and Slack handlers call Calendar concurrently. The timeout keeps a hung request from
    freezing the scheduler."""
    if getattr(_local, "service", None) is None:
        http = AuthorizedHttp(get_credentials(), http=httplib2.Http(timeout=HTTP_TIMEOUT_SECONDS))
        _local.service = build("calendar", "v3", http=http, cache_discovery=False)
    return _local.service


@cache
def timezone_of(calendar_id: str) -> ZoneInfo:
    return ZoneInfo(service().calendars().get(calendarId=calendar_id).execute()["timeZone"])


def events_between(calendar_id: str, start: dt.datetime, end: dt.datetime) -> list[dict]:
    """Timed, non-transparent events overlapping [start, end], sorted by start."""
    items = service().events().list(
        calendarId=calendar_id,
        timeMin=start.isoformat(),
        timeMax=(end + dt.timedelta(seconds=1)).isoformat(),  # timeMax is exclusive; include events starting at `end`
        singleEvents=True,
        orderBy="startTime",
    ).execute().get("items", [])
    events = []
    for e in items:
        if "dateTime" not in e.get("start", {}) or e.get("transparency") == "transparent":
            continue
        events.append({
            "id": e["id"],
            "summary": e.get("summary", "(no title)"),
            "start": _ts(e["start"]["dateTime"]),
            "end": _ts(e["end"]["dateTime"]),
            # Radar's own review blocks: busy time, but not a call (older blocks have no tag, only the title)
            "review_block": e.get("extendedProperties", {}).get("private", {}).get("radar") == "review-block"
            or e.get("summary", "").startswith(REVIEW_BLOCK_PREFIX),
        })
    return events


def busy_blocks(emails: list[str], start: dt.datetime, end: dt.datetime) -> dict[str, list[Block]]:
    """Free/busy per calendar. Calendars the bot can't see are reported as busy for the whole range."""
    result = service().freebusy().query(body={
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "items": [{"id": e} for e in emails],
    }).execute()["calendars"]
    blocks: dict[str, list[Block]] = {}
    for email in emails:
        info = result.get(email, {})
        if info.get("errors"):
            blocks[email] = [(start, end)]
        else:
            blocks[email] = [(_ts(b["start"]), _ts(b["end"])) for b in info.get("busy", [])]
    return blocks


def create_review_block(calendar_id: str, start: dt.datetime, end: dt.datetime, summary: str, description: str) -> str:
    event = service().events().insert(calendarId=calendar_id, body={
        "summary": summary,
        "description": description,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 5}]},
        "extendedProperties": {"private": REVIEW_BLOCK_TAG},
    }).execute()
    return event.get("htmlLink", "")

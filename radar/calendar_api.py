"""Google Calendar access (as the bot account) plus pure helpers for busy/free math. All datetimes are timezone-aware."""
from __future__ import annotations

import datetime as dt
from functools import cache
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

from radar.google_auth import get_credentials

Block = tuple[dt.datetime, dt.datetime]
REVIEW_BLOCK_TAG = {"radar": "review-block"}
REVIEW_BLOCK_PREFIX = "🔍 Review PR"
MERGE_GAP = dt.timedelta(minutes=5)
WORK_START_HOUR = 9
WORK_END_HOUR = 20


def _ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def merge(blocks: list[Block]) -> list[Block]:
    merged: list[Block] = []
    for start, end in sorted(blocks):
        if merged and start - merged[-1][1] <= MERGE_GAP:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def busy_until(blocks: list[Block], at: dt.datetime) -> dt.datetime | None:
    """End of the busy stretch containing `at`, or None if free at `at`."""
    for start, end in merge(blocks):
        if start <= at < end:
            return end
    return None


def is_free(blocks: list[Block], start: dt.datetime, end: dt.datetime) -> bool:
    return not any(s < end and start < e for s, e in blocks)


def first_free_slot(blocks: list[Block], after: dt.datetime, minutes: int, tz: dt.tzinfo, days: int = 2) -> Block | None:
    """First gap of `minutes` within working hours (in `tz`) starting at or after `after`."""
    length = dt.timedelta(minutes=minutes)
    busy = merge(blocks)
    local_after = after.astimezone(tz)
    for day in range(days + 1):
        date = (local_after + dt.timedelta(days=day)).date()
        window_start = dt.datetime.combine(date, dt.time(WORK_START_HOUR), tz)
        window_end = dt.datetime.combine(date, dt.time(WORK_END_HOUR), tz)
        cursor = max(window_start, after)
        for start, end in busy:
            if end <= cursor:
                continue
            if start - cursor >= length:
                break
            cursor = max(cursor, end)
        if cursor + length <= window_end:
            return cursor, cursor + length
    return None


@cache
def service():
    return build("calendar", "v3", credentials=get_credentials(), cache_discovery=False)


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

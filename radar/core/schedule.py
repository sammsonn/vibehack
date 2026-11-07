"""Pure busy/free math on calendar blocks. All datetimes are timezone-aware."""
from __future__ import annotations

import datetime as dt

Block = tuple[dt.datetime, dt.datetime]
MERGE_GAP = dt.timedelta(minutes=5)
WORK_START_HOUR = 9
WORK_END_HOUR = 20


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

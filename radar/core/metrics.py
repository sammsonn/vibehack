"""The number, computed: time to first review with vs without Radar, plus Radar's decision log. No I/O."""
from __future__ import annotations

import datetime as dt
import statistics
from collections import Counter
from dataclasses import dataclass

APPROVED = ("handed_off", "blocked")
DECIDED = ("handed_off", "blocked", "kept", "snoozed", "stale")


@dataclass(frozen=True)
class ReviewRecord:
    number: int
    title: str
    author: str
    start: dt.datetime  # opened, or last marked ready for review
    first_review_at: dt.datetime | None
    first_reviewer: str | None
    open: bool


@dataclass(frozen=True)
class Stats:
    without_radar: list[dt.timedelta]
    with_radar: list[dt.timedelta]
    still_waiting: list[tuple[int, dt.timedelta, bool]]  # (pr, waited so far, radar acted)
    status_counts: Counter
    decision_times: list[dt.timedelta]
    radar_prs: set[int]


def _ts(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value) if value else None


def compute(records: list[ReviewRecord], suggestions: list, now: dt.datetime) -> Stats:
    """suggestions: rows from store.all_suggestions(). A PR counts as 'with Radar' if the lead approved a
    Radar action on it before its first review."""
    approved_at: dict[int, dt.datetime] = {}
    for row in suggestions:
        if row["status"] in APPROVED and row["decided_at"]:
            t = _ts(row["decided_at"])
            approved_at[row["pr_number"]] = min(t, approved_at.get(row["pr_number"], t))

    without, with_, waiting, radar_prs = [], [], [], set()
    for r in records:
        acted = r.number in approved_at and (r.first_review_at is None or approved_at[r.number] <= r.first_review_at)
        if acted:
            radar_prs.add(r.number)
        if r.first_review_at:
            (with_ if acted else without).append(r.first_review_at - r.start)
        elif r.open:
            waiting.append((r.number, now - r.start, acted))

    counts = Counter(row["status"] for row in suggestions)
    decision_times = [
        _ts(row["decided_at"]) - _ts(row["suggested_at"])
        for row in suggestions if row["status"] in DECIDED and row["decided_at"]
    ]
    return Stats(without, with_, sorted(waiting, key=lambda w: -w[1]), counts, decision_times, radar_prs)


def fmt(delta: dt.timedelta | None) -> str:
    if delta is None:
        return "n/a"
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours < 48 else f"{hours // 24}d {hours % 24}h"


def median(values: list[dt.timedelta]) -> dt.timedelta | None:
    return statistics.median(values) if values else None


def render(st: Stats, repo_name: str, days: int) -> str:
    def line(label: str, values: list[dt.timedelta]) -> str:
        return f"• {label}: *{fmt(median(values))}* median (n={len(values)})"

    c = st.status_counts
    suggested = sum(c.values()) - c["superseded"]
    lines = [
        f"*🛰️ Review Radar stats* · `{repo_name}` · last {days} days",
        "*Time to first review* (opened → first review by someone other than the author, bots excluded)",
        line("Without Radar", st.without_radar),
        line("With Radar", st.with_radar),
    ]
    if st.still_waiting:
        longest = ", ".join(f"#{n} {fmt(w)}{' (Radar acted)' if acted else ''}" for n, w, acted in st.still_waiting[:3])
        lines.append(f"• Still waiting: {len(st.still_waiting)} PR{'s' if len(st.still_waiting) != 1 else ''} ({longest})")
    lines += [
        "*Radar decisions*",
        f"• {suggested} suggested · {c['handed_off'] + c['blocked']} approved "
        f"({c['handed_off']} handoff{'s' if c['handed_off'] != 1 else ''}, {c['blocked']} review block{'s' if c['blocked'] != 1 else ''})"
        f" · {c['kept']} kept · {c['snoozed']} snoozed · {c['pending']} open",
        f"• Lead's time from card to decision: *{fmt(median(st.decision_times))}* median",
    ]
    return "\n".join(lines)

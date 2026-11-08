"""App Home dashboard math: who waits on whom, load per person, handoffs, bottlenecks. Pure: no I/O."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from radar.config import Team
from radar.core import metrics, schedule
from radar.core.models import Suggestion, Triage


@dataclass(frozen=True)
class OpenPR:
    number: int
    title: str
    url: str
    author: str
    draft: bool
    created_at: dt.datetime
    requested: dict[str, dt.datetime] = field(default_factory=dict)  # lowercase login -> last asked to review


@dataclass(frozen=True)
class Handoff:
    pr_number: int
    title: str
    url: str
    to: str  # GitHub login
    at: dt.datetime
    state: str  # "waiting" | "reviewed" | "closed"


@dataclass(frozen=True)
class Load:
    github: str
    name: str
    reviews: int  # open review requests on non-draft PRs
    authored: int  # own open PRs
    oldest_wait: dt.timedelta | None
    busy_until: dt.datetime | None  # None = free now


@dataclass(frozen=True)
class Snapshot:
    at: dt.datetime
    horizon: dt.datetime  # end of the calendar window that was queried
    prs: list[OpenPR]
    busy: dict[str, list[schedule.Block]]  # email -> busy blocks
    handoffs: list[Handoff]
    stats: metrics.Stats | None = None
    savings: metrics.Savings | None = None
    stats_days: int = 7
    buy: str | None = None
    pending: list[tuple[int, Suggestion]] = field(default_factory=list)  # (card id, suggestion) awaiting the lead
    snoozed: list[tuple[Suggestion, dt.datetime]] = field(default_factory=list)
    triage: dict[int, Triage] = field(default_factory=dict)  # PR -> Radar's latest risk read
    review_blocks: dict[int, schedule.Block] = field(default_factory=dict)  # PR -> booked block in the lead's calendar
    lead_status: str = ""
    gates: dict[int, str] = field(default_factory=dict)  # PR -> radar/approval state


def pending(rows) -> list[tuple[int, Suggestion]]:
    return [(row["card_id"], Suggestion.from_json(row["payload"])) for row in rows if row["status"] == "pending"]


def snoozed(rows, now: dt.datetime) -> list[tuple[Suggestion, dt.datetime]]:
    """PRs still snoozed at `now`, latest snooze per PR, soonest to wake first."""
    latest: dict[int, tuple[Suggestion, dt.datetime]] = {}
    for row in rows:
        if row["status"] == "snoozed" and row["snoozed_until"]:
            until = dt.datetime.fromisoformat(row["snoozed_until"])
            if until > now:
                latest[row["pr_number"]] = (Suggestion.from_json(row["payload"]), until)
    return sorted(latest.values(), key=lambda su: su[1])


def latest_triage(rows) -> dict[int, Triage]:
    """Radar's most recent risk read per PR. rows are ordered oldest first."""
    return {row["pr_number"]: Suggestion.from_json(row["payload"]).triage for row in rows}


def waiting_on(snap: Snapshot, login: str) -> list[tuple[OpenPR, dt.timedelta]]:
    """Non-draft PRs where `login` is a requested reviewer, longest wait first."""
    login = login.lower()
    result = [(p, snap.at - p.requested[login]) for p in snap.prs if not p.draft and login in p.requested]
    return sorted(result, key=lambda pw: -pw[1])


def authored_by(snap: Snapshot, login: str) -> list[OpenPR]:
    return sorted((p for p in snap.prs if p.author.lower() == login.lower()), key=lambda p: p.created_at)


def team_load(snap: Snapshot, team: Team) -> list[Load]:
    """Every member, most open review requests first."""
    loads = []
    for m in team.members:
        waits = [w for _, w in waiting_on(snap, m.github)]
        loads.append(Load(
            github=m.github,
            name=m.name,
            reviews=len(waits),
            authored=len(authored_by(snap, m.github)),
            oldest_wait=max(waits, default=None),
            busy_until=schedule.busy_until(snap.busy.get(m.email, []), snap.at),
        ))
    return sorted(loads, key=lambda ld: (-ld.reviews, ld.name))


def stale(snap: Snapshot, hours: float, lead: str = "") -> list[tuple[OpenPR, str, dt.timedelta]]:
    """(PR, reviewer login, wait) for every review request older than `hours`, longest first. Requests on the
    `lead` for PRs with a booked review block are decided, so they don't count."""
    limit = dt.timedelta(hours=hours)
    result = [(p, login, snap.at - since) for p in snap.prs if not p.draft
              for login, since in p.requested.items() if snap.at - since >= limit
              and not (login == lead.lower() and p.number in snap.review_blocks)]
    return sorted(result, key=lambda r: -r[2])


def handoffs(rows, prs: list[OpenPR], since: dt.datetime) -> list[Handoff]:
    """Radar handoffs the lead approved since `since`, newest first. rows: store.all_suggestions()."""
    open_prs = {p.number: p for p in prs}
    result = []
    for row in rows:
        if row["status"] != "handed_off" or not row["decided_at"]:
            continue
        at = dt.datetime.fromisoformat(row["decided_at"])
        if at < since:
            continue
        sg = Suggestion.from_json(row["payload"])
        login = row["handed_to"] or (sg.reviewer.github if sg.reviewer else "")
        if not login:
            continue
        p = open_prs.get(row["pr_number"])
        # GitHub drops the review request once the reviewer submits a review.
        state = "closed" if p is None else "waiting" if login.lower() in p.requested else "reviewed"
        result.append(Handoff(sg.pr.number, sg.pr.title, sg.pr.url, login, at, state))
    return sorted(result, key=lambda h: h.at, reverse=True)

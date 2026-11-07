"""Pure decision logic: who should review, and whether to hand off or keep the PR with the lead."""
import datetime as dt
from collections import Counter

from radar import calendar_api
from radar.config import Team
from radar.models import PR, Candidate, Suggestion, Triage

FREE_HORIZON = dt.timedelta(minutes=60)


def rank(pr: PR, team: Team, experts: Counter, open_reviews: Counter,
         busy: dict[str, list[calendar_api.Block]], now: dt.datetime) -> list[Candidate]:
    """Teammates who can review `pr` (never the lead or the author), best first:
    free in the next hour, then most commits to the PR's files, then fewest open reviews."""
    candidates = []
    for m in team.teammates:
        if m.github.lower() == pr.author.lower():
            continue
        blocks = busy.get(m.email, [])
        candidates.append(Candidate(
            github=m.github,
            name=m.name,
            free=calendar_api.is_free(blocks, now, now + FREE_HORIZON),
            file_commits=experts.get(m.github.lower(), 0),
            open_reviews=open_reviews.get(m.github.lower(), 0),
            busy_until=calendar_api.busy_until(blocks, now),
        ))
    return sorted(candidates, key=lambda c: (not c.free, -c.file_commits, c.open_reviews, c.name.lower()))


def decide(pr: PR, triage: Triage, ranked: list[Candidate],
           lead_slot: calendar_api.Block | None, lead_busy_until: dt.datetime | None) -> Suggestion:
    best = ranked[0] if ranked else None
    if triage.risk == "low" and best and best.free:
        return Suggestion(pr, triage, "handoff", f"Low risk, {best.name} is {best.why}", best, None, lead_busy_until,
                          tuple(ranked))
    if triage.risk == "high":
        why = "High risk, stays with you"
    elif not ranked:
        why = "No teammate can review it (the author is the only other person)"
    else:
        why = f"No free teammate ({', '.join(c.name for c in ranked)} busy)"
    return Suggestion(pr, triage, "block", why, None, lead_slot, lead_busy_until, tuple(ranked))

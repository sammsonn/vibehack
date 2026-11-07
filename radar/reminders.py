"""One nudge per card: when the lead is free again and Radar's suggestions are still unclicked."""
import datetime as dt
import logging

from radar import github_api, store
from radar.config import Settings
from radar.models import Suggestion

log = logging.getLogger(__name__)
REMIND_AFTER_FREE_CARD = dt.timedelta(minutes=30)


def remind_at(created_at: dt.datetime, lead_busy_until: dt.datetime | None) -> dt.datetime:
    """Cards posted before a call: remind when the call stretch ends. Cards posted while free: after 30 min."""
    return lead_busy_until if lead_busy_until else created_at + REMIND_AFTER_FREE_CARD


def due(s: Settings, now: dt.datetime) -> list[tuple[int, list[Suggestion]]]:
    """Cards to remind about now, with their still-valid pending suggestions. Call only while the lead is free.
    PRs already reviewed or closed are marked stale instead of being nagged about."""
    lead = s.team.lead.github
    result = []
    for card_id, rows in store.pending_by_card().items():
        card = store.card(card_id)
        if not card["ts"]:
            continue
        suggestions = {row["id"]: Suggestion.from_json(row["payload"]) for row in rows}
        busy_until = max((sg.lead_busy_until for sg in suggestions.values() if sg.lead_busy_until), default=None)
        if now < remind_at(dt.datetime.fromisoformat(card["created_at"]), busy_until):
            continue
        if not store.fire_once(f"remind:{card_id}"):
            continue
        repo = github_api.live_repo(s)
        still_pending = []
        for sid, sg in suggestions.items():
            if github_api.still_waiting_on(repo, sg.pr.number, lead) is None:
                if store.claim(sid, "radar"):
                    store.finish(sid, "stale", f"#{sg.pr.number} was reviewed or closed before you clicked")
            else:
                still_pending.append(sg)
        log.info("reminder for card %s: %d PRs still pending", card_id, len(still_pending))
        result.append((card_id, still_pending))
    return result

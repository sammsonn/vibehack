"""Merge gate: a `radar/approval` commit status that is green only when the lead, or a teammate the lead
handed the PR to, approved the PR's latest commit. Make it a required status check in the branch ruleset."""
import json
import logging
import threading

from radar.config import Settings, Team
from radar.integrations import github
from radar.storage import store

log = logging.getLogger(__name__)
CONTEXT = "radar/approval"
DECISIVE = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")
SYNC_SECONDS = 60


def allowed_approvers(team: Team, author: str, handed_off_to: set[str]) -> set[str]:
    """Lowercase logins whose approval opens the gate. Nobody can open it for their own PR."""
    lead = team.lead.github.lower()
    if author.lower() == lead:
        allowed = {m.github.lower() for m in team.teammates}
    else:
        allowed = {lead}
    return (allowed | handed_off_to) - {author.lower()}


def approvers_of_head(reviews, head_sha: str) -> set[str]:
    """Logins whose latest decisive review is an approval of `head_sha` (approvals of older commits don't count)."""
    latest = {}
    for r in sorted((r for r in reviews if r.user and r.submitted_at), key=lambda r: r.submitted_at):
        if r.state in DECISIVE:
            latest[r.user.login.lower()] = r
    return {login for login, r in latest.items() if r.state == "APPROVED" and r.commit_id == head_sha}


def evaluate(allowed: set[str], approved: set[str]) -> tuple[str, str]:
    ok = sorted(allowed & approved)
    if ok:
        return "success", f"Approved by @{ok[0]}"
    if not allowed:
        return "pending", "No eligible approver"
    return "pending", f"Needs approval from {', '.join('@' + a for a in sorted(allowed))}"[:140]


def _handed_off_to(pr_number: int) -> set[str]:
    """Who the lead actually handed the PR to: his own pick from the ranking, else Radar's suggestion."""
    logins = set()
    for row in store.handed_off(pr_number):
        login = row["handed_to"] or (json.loads(row["payload"]).get("reviewer") or {}).get("github")
        if login:
            logins.add(login.lower())
    return logins


def sync(s: Settings, numbers: list[int] | None = None) -> None:
    """Posts the gate status on open PRs (only `numbers` if given); skips PRs whose status is already current."""
    repo = github.live_repo(s)
    for p in repo.get_pulls(state="open"):
        if numbers and p.number not in numbers:
            continue
        head = p.head.sha
        allowed = allowed_approvers(s.team, p.user.login, _handed_off_to(p.number))
        state, description = evaluate(allowed, approvers_of_head(p.get_reviews(), head))
        commit = repo.get_commit(head)
        current = next((st for st in commit.get_statuses() if st.context == CONTEXT), None)
        if current and current.state == state and current.description == description:
            continue
        commit.create_status(state=state, description=description, context=CONTEXT)
        log.info("gate #%s: %s (%s)", p.number, state, description)


def loop(s: Settings, stop: threading.Event) -> None:
    while True:
        try:
            sync(s)
        except Exception:
            log.exception("approval gate sync failed")
        if stop.wait(SYNC_SECONDS):
            return

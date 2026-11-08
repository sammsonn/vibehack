"""App Home tab: the Radar dashboard, in the same style as Radar's cards, digest and /radar stats.
Pure: takes a snapshot, returns a Block Kit view. Every developer sees what waits on them, their load, their
PRs and their handoffs; the lead also sees the decisions waiting on his click, team load, bottlenecks and
the live number."""
import datetime as dt

from radar.config import Member, Team
from radar.core import dashboard, metrics
from radar.core.dashboard import Load, OpenPR, Snapshot
from radar.core.models import Suggestion
from radar.core.schedule import describe_slot
from radar.slack.blocks import duration

MAX_ROWS = 8
BAR_WIDTH = 10
CONCENTRATION = 0.5  # one person holding this share of all open review requests is a bottleneck
MIN_REQUESTS = 3  # ...once there are at least this many
RISK_ICON = {"high": "🔴", "low": "🟢"}
GATE = {"success": "✅ mergeable", "pending": "🔒 needs approval", "failure": "⛔ merge gate failed",
        "error": "⛔ merge gate failed"}


def _s(n: int) -> str:
    return "" if n == 1 else "s"


def bar(value: float, maximum: float, width: int = BAR_WIDTH) -> str:
    filled = 0 if maximum <= 0 else round(min(value, maximum) / maximum * width)
    if value > 0:
        filled = max(filled, 1)
    return "▰" * filled + "▱" * (width - filled)


def availability(ld: Load, snap: Snapshot, tz: dt.tzinfo) -> str:
    if ld.busy_until is None:
        return "free"
    if ld.busy_until >= snap.horizon:
        return "busy all day"
    return f"busy until {ld.busy_until.astimezone(tz):%H:%M}"


def _section(text: str, accessory: dict | None = None) -> dict:
    block = {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}
    if accessory:
        block["accessory"] = accessory
    return block


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text[:2900]}]}


def _button(text: str, action_id: str, url: str | None = None, style: str | None = None) -> dict:
    b = {"type": "button", "text": {"type": "plain_text", "text": text, "emoji": True}, "action_id": action_id}
    if url:
        b["url"] = url
    if style:
        b["style"] = style
    return b


DIVIDER = {"type": "divider"}


def _waiting(snap: Snapshot, viewer: Member, team: Team, is_lead: bool, stale_hours: float,
             tz: dt.tzinfo) -> list[dict]:
    items = dashboard.waiting_on(snap, viewer.github)
    if not items:
        return [_section("*Waiting on you*\nNothing is waiting on your review.")]
    blocks = [_section(f"*Waiting on you* · {len(items)} PR{_s(len(items))}")]
    for p, wait in items[:MAX_ROWS]:
        tr = snap.triage.get(p.number)
        waited = f"*{duration(wait)} waiting*" if wait.total_seconds() >= stale_hours * 3600 else f"{duration(wait)} waiting"
        lines = [f"{RISK_ICON.get(tr.risk, '') + ' ' if tr else ''}*<{p.url}|#{p.number} {p.title}>* "
                 f"({team.display_name(p.author)}, {waited})"]
        if tr:
            look = f" Look first: `{tr.look_first}`" if tr.look_first else ""
            lines.append(f"*{tr.risk.capitalize()} risk:* {tr.risk_reason.rstrip('.')}.{look}")
        if is_lead and p.number in snap.review_blocks:
            lines.append(f"📅 Review block booked {describe_slot(snap.review_blocks[p.number], tz, snap.at)}")
        blocks.append(_section("\n".join(lines), _button("Review →", f"home:open:{p.number}", url=f"{p.url}/files")))
    if len(items) > MAX_ROWS:
        blocks.append(_context(f"+{len(items) - MAX_ROWS} more"))
    return blocks


def _suggestion(sg: Suggestion, tz: dt.tzinfo, now: dt.datetime) -> str:
    if sg.action == "handoff" and sg.reviewer:
        return f"→ Hand off to *{sg.reviewer.name}*"
    if sg.slot:
        return f"→ Block *{describe_slot(sg.slot, tz, now)}* to review"
    return "→ No free slot left in working hours"


def _decisions(snap: Snapshot, tz: dt.tzinfo, links: dict[int, str]) -> list[dict]:
    if not snap.pending:
        blocks = [_section("*Waiting for your decision*\nNothing to decide right now.")]
    else:
        blocks = [_section(f"*Waiting for your decision* · {len(snap.pending)} PR{_s(len(snap.pending))}")]
    for card_id, sg in snap.pending[:MAX_ROWS]:
        link = links.get(card_id)
        blocks.append(_section(
            f"{RISK_ICON.get(sg.triage.risk, '')} *<{sg.pr.url}|#{sg.pr.number} {sg.pr.title}>*\n"
            f"{_suggestion(sg, tz, snap.at)}",
            _button("Open card", f"home:open:card{card_id}:{sg.pr.number}", url=link) if link else None,
        ))
    extra = []
    if snap.review_blocks:
        booked = sorted(snap.review_blocks.items(), key=lambda kv: kv[1])
        extra.append("📅 Review blocks: " + ", ".join(f"#{n} {describe_slot(b, tz, snap.at)}" for n, b in booked))
    if snap.snoozed:
        extra.append("⏰ Snoozed: " + ", ".join(f"#{sg.pr.number} until {until.astimezone(tz):%H:%M}"
                                               for sg, until in snap.snoozed))
    if extra:
        blocks.append(_context("  ·  ".join(extra)))
    return blocks


def _my_pr_line(p: OpenPR, snap: Snapshot, team: Team) -> str:
    link = f"<{p.url}|#{p.number} {p.title}>"
    gate = snap.gates.get(p.number)
    if p.draft:
        return f"• {link}: draft"
    if p.requested:
        status = "waiting on " + ", ".join(
            f"{team.display_name(login)} ({duration(snap.at - since)})"
            for login, since in sorted(p.requested.items(), key=lambda kv: kv[1]))
    elif gate == "success":
        status = "approved"
    else:
        status = "no reviewer requested"
    return f"• {link}: {status}" + (f" · {GATE[gate]}" if gate in GATE else "")


def _my_load(snap: Snapshot, viewer: Member, team: Team, is_lead: bool, loads: list[Load],
             tz: dt.tzinfo) -> list[dict]:
    ld = next(x for x in loads if x.github == viewer.github)
    mine = dashboard.authored_by(snap, viewer.github)
    calendar = availability(ld, snap, tz).capitalize()
    if is_lead and snap.lead_status:
        calendar = snap.lead_status
    oldest = f" (oldest {duration(ld.oldest_wait)})" if ld.oldest_wait else ""
    lines = [
        "*Your load*",
        f"• Review requests on you: *{ld.reviews}*{oldest}",
        f"• Your open PRs: *{ld.authored}*",
        f"• Calendar: {calendar}",
    ]
    blocks = [_section("\n".join(lines))]
    if mine:
        blocks.append(_section("*Your PRs*\n" + "\n".join(_my_pr_line(p, snap, team) for p in mine[:MAX_ROWS])))
    return blocks


def _handoff_line(h: dashboard.Handoff, team: Team, is_lead: bool) -> str:
    name = team.display_name(h.to)
    if h.state == "closed":
        icon, text = "🏁", "closed"
    elif h.state == "reviewed":
        icon, text = "✅", f"{name} reviewed it" if is_lead else "you reviewed it"
    else:
        icon, text = "👀", f"{name} is on it (no review yet)" if is_lead else "waiting on your review"
    return f"{icon} <{h.url}|#{h.pr_number} {h.title}>: {text}"


def _handoffs(snap: Snapshot, viewer: Member, team: Team, is_lead: bool) -> list[dict]:
    items = snap.handoffs if is_lead else [h for h in snap.handoffs if h.to.lower() == viewer.github.lower()]
    title = "*Your handoffs* · last 7 days" if is_lead else f"*Handed to you by {team.lead.name}* · last 7 days"
    if not items:
        return [_section(f"{title}\nNo Radar handoffs.")]
    return [_section(title + "\n" + "\n".join(_handoff_line(h, team, is_lead) for h in items[:MAX_ROWS]))]


def _team_load(snap: Snapshot, loads: list[Load], tz: dt.tzinfo) -> list[dict]:
    most = max(max(x.reviews for x in loads), 1)
    lines = ["*Team load* · open review requests per person"]
    for ld in loads:
        oldest = f", oldest {duration(ld.oldest_wait)}" if ld.oldest_wait else ""
        lines.append(f"{bar(ld.reviews, most)}  *{ld.reviews}*  {ld.name} ({availability(ld, snap, tz)}{oldest})")
    return [_section("\n".join(lines))]


def _bottlenecks(snap: Snapshot, team: Team, loads: list[Load], stale_hours: float, tz: dt.tzinfo) -> list[dict]:
    lines = []
    old = dashboard.stale(snap, stale_hours, team.lead.github)
    if old:
        listed = ", ".join(f"<{p.url}|#{p.number}> on {team.display_name(login)} ({duration(w)})" for p, login, w in old[:5])
        lines.append(f"• {len(old)} review request{_s(len(old))} older than {stale_hours:g}h: {listed}")
    total = sum(x.reviews for x in loads)
    top = loads[0]
    if total >= MIN_REQUESTS and top.reviews / total >= CONCENTRATION:
        lines.append(f"• {top.name} holds {top.reviews} of {total} open review requests ({top.reviews / total:.0%})")
    for ld in loads:
        if ld.reviews and ld.busy_until:
            lines.append(f"• {ld.name} has {ld.reviews} review{_s(ld.reviews)} waiting and is {availability(ld, snap, tz)}")
    return [_section("*Bottlenecks*\n" + ("\n".join(lines) or "None right now."))]


def _number(snap: Snapshot) -> list[dict]:
    period = "this week" if snap.stats_days == 7 else f"last {snap.stats_days} days"
    title = f"*The number* · {period}"
    st, sv = snap.stats, snap.savings
    if st is None or sv is None:
        return [_section(f"{title}\nStats are unavailable right now. Try `/radar stats`.")]
    without, with_ = metrics.median(st.without_radar), metrics.median(st.with_radar)
    secs = lambda d: d.total_seconds() if d else 0  # noqa: E731
    scale = max(secs(without), secs(with_))
    lines = [
        title,
        "Median time to first review:",
        f"{bar(secs(without), scale)}  *{metrics.fmt(without)}*  without Radar (n={len(st.without_radar)})",
        f"{bar(secs(with_), scale)}  *{metrics.fmt(with_)}*  with Radar (n={len(st.with_radar)})",
    ]
    if sv.prs:
        lines.append(metrics.savings_lines(sv, snap.stats_days)[0])
    else:
        lines.append(f"💰 No PR has been reviewed after a Radar action {period} yet, so there are no savings to show.")
    if snap.buy:
        lines.append(snap.buy)
    return [_section("\n".join(lines)), _context(
        f"Radar decisions: {metrics.decisions_summary(st.status_counts)} · "
        f"your time from card to decision: {metrics.fmt(metrics.median(st.decision_times))} median")]


def render(snap: Snapshot, viewer: Member, team: Team, tz: dt.tzinfo, stale_hours: float,
           links: dict[int, str] | None = None) -> dict:
    """links: card id -> Slack permalink of that card, for the lead's 'Open card' buttons."""
    is_lead = viewer.github.lower() == team.lead.github.lower()
    loads = dashboard.team_load(snap, team)
    buttons = [_button("🔄 Refresh", "home:refresh")]
    if is_lead:
        buttons.append(_button("🛰️ Sweep now", "home:sweep", style="primary"))
    blocks = [
        _section(f"*🛰️ Review Radar* · {viewer.name}"),
        _context(f"{'Engineering lead' if is_lead else 'Developer'} · updated {snap.at.astimezone(tz):%H:%M}"),
        {"type": "actions", "elements": buttons},
        DIVIDER,
        *_waiting(snap, viewer, team, is_lead, stale_hours, tz),
    ]
    if is_lead:
        blocks += [DIVIDER, *_decisions(snap, tz, links or {})]
    blocks += [
        DIVIDER,
        *_my_load(snap, viewer, team, is_lead, loads, tz),
        DIVIDER,
        *_handoffs(snap, viewer, team, is_lead),
    ]
    if is_lead:
        blocks += [DIVIDER, *_team_load(snap, loads, tz), DIVIDER, *_bottlenecks(snap, team, loads, stale_hours, tz),
                   DIVIDER, *_number(snap)]
    return {"type": "home", "blocks": blocks[:100]}


def message_view(text: str) -> dict:
    """A one-message Home tab: unknown users, or when the snapshot failed."""
    return {"type": "home", "blocks": [_section("*🛰️ Review Radar*"), _section(text)]}

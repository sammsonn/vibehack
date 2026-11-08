"""App Home tab: the Radar dashboard. Pure: takes a snapshot, returns a Block Kit view.
Every developer sees what waits on them, their load, their PRs and their handoffs; the lead also sees the
decisions waiting on his click, team load, bottlenecks and the live number."""
import datetime as dt

from radar.config import Member, Team
from radar.core import dashboard, metrics
from radar.core.dashboard import Load, OpenPR, Snapshot
from radar.core.schedule import describe_slot
from radar.slack.blocks import duration, primary_label

MAX_ROWS = 8
BAR_WIDTH = 5
FULL_LOAD = 5  # open review requests that fill the load bar
CONCENTRATION = 0.5  # one person holding this share of all open review requests is a bottleneck
MIN_REQUESTS = 3  # ...once there are at least this many
RISK = {"high": "🔴 High risk", "low": "🟢 Low risk"}
GATE = {"success": "✅ mergeable", "pending": "🔒 needs approval", "failure": "⛔ gate failed", "error": "⛔ gate failed"}
HANDOFF_STATE = {
    "waiting": ("🟨", "👀", "waiting for review"),
    "reviewed": ("🟩", "✅", "reviewed"),
    "closed": ("⬜", "🏁", "closed"),
}


def _s(n: int) -> str:
    return "" if n == 1 else "s"


def bar(value: float, maximum: float, fill: str = "🟦", width: int = BAR_WIDTH) -> str:
    filled = 0 if maximum <= 0 else round(min(value, maximum) / maximum * width)
    if value > 0:
        filled = max(filled, 1)
    return fill * filled + "⬜" * (width - filled)


def wait_bar(wait: dt.timedelta, stale_hours: float) -> str:
    hours = wait.total_seconds() / 3600
    color = "🟥" if hours >= stale_hours else "🟨" if hours >= stale_hours / 2 else "🟩"
    return bar(hours, stale_hours, color)


def load_bar(reviews: int) -> str:
    return bar(reviews, FULL_LOAD, "🟩" if reviews <= 2 else "🟨" if reviews <= 4 else "🟥")


def availability(ld: Load, snap: Snapshot, tz: dt.tzinfo) -> str:
    if ld.busy_until is None:
        return "🟢 free"
    if ld.busy_until >= snap.horizon:
        return "🔴 busy all day"
    return f"🔴 busy until {ld.busy_until.astimezone(tz):%H:%M}"


def _header(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": text[:150], "emoji": True}}


def _section(text: str, accessory: dict | None = None) -> dict:
    block = {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}
    if accessory:
        block["accessory"] = accessory
    return block


def _fields(*texts: str) -> dict:
    return {"type": "section", "fields": [{"type": "mrkdwn", "text": t[:1900]} for t in texts]}


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
    blocks = [_header(f"👀 Waiting on you · {len(items)}")]
    if not items:
        return blocks + [_section("🎉 Nothing is waiting on your review. Inbox zero.")]
    over = sum(1 for _, w in items if w.total_seconds() >= stale_hours * 3600)
    blocks.append(_context(f"Oldest *{duration(items[0][1])}*" + (f" · {over} over {stale_hours:g}h" if over else "")))
    for p, wait in items[:MAX_ROWS]:
        tr = snap.triage.get(p.number)
        lines = [f"*<{p.url}|#{p.number} {p.title}>*" + (f"  ·  {RISK.get(tr.risk, '')}" if tr else ""),
                 f"by {team.display_name(p.author)} · ⏱️ *{duration(wait)}* {wait_bar(wait, stale_hours)}"]
        if tr and tr.look_first:
            lines.append(f"👉 Look first: `{tr.look_first}`")
        if is_lead and p.number in snap.review_blocks:
            lines.append(f"📅 Review block booked {describe_slot(snap.review_blocks[p.number], tz, snap.at)}")
        blocks.append(_section("\n".join(lines), _button("Review →", f"home:open:{p.number}", url=f"{p.url}/files")))
    if len(items) > MAX_ROWS:
        blocks.append(_context(f"+{len(items) - MAX_ROWS} more"))
    half = stale_hours / 2
    blocks.append(_context(f"Bar fills at {stale_hours:g}h · 🟩 under {half:g}h · 🟨 {half:g}–{stale_hours:g}h · "
                           f"🟥 over {stale_hours:g}h"))
    return blocks


def _decisions(snap: Snapshot, tz: dt.tzinfo, links: dict[int, str]) -> list[dict]:
    blocks = [_header(f"🗳️ Waiting for your decision · {len(snap.pending)}")]
    if not snap.pending:
        blocks.append(_section("✅ Nothing to decide right now. Radar posts a card before your next call."))
    for card_id, sg in snap.pending[:MAX_ROWS]:
        label = primary_label(sg, tz, snap.at) or "No free slot left: keep it or pick a reviewer"
        link = links.get(card_id)
        blocks.append(_section(
            f"{'🔴' if sg.triage.risk == 'high' else '🟢'} *<{sg.pr.url}|#{sg.pr.number} {sg.pr.title}>*\n"
            f"Radar suggests: *{label}*",
            _button("Open card →", f"home:open:card{card_id}:{sg.pr.number}", url=link) if link else None,
        ))
    extra = []
    if snap.review_blocks:
        booked = sorted(snap.review_blocks.items(), key=lambda kv: kv[1])
        extra.append("📅 Review blocks: " + ", ".join(f"#{n} {describe_slot(b, tz, snap.at)}" for n, b in booked))
    if snap.snoozed:
        extra.append("⏰ Snoozed: " + ", ".join(f"#{sg.pr.number} until {until.astimezone(tz):%H:%M}"
                                               for sg, until in snap.snoozed))
    if extra:
        blocks.append(_context("\n".join(extra)))
    return blocks


def _my_pr_line(p: OpenPR, snap: Snapshot, team: Team) -> str:
    link = f"<{p.url}|#{p.number} {p.title}>"
    if p.draft:
        return f"✏️ {link} · draft"
    gate = f" · {GATE[snap.gates[p.number]]}" if snap.gates.get(p.number) in GATE else ""
    if not p.requested:
        return f"⚪ {link} · no reviewer requested{gate}"
    on = ", ".join(f"{team.display_name(login)} {duration(snap.at - since)}" for login, since in
                   sorted(p.requested.items(), key=lambda kv: kv[1]))
    return f"⏳ {link} · waiting on {on}{gate}"


def _my_load(snap: Snapshot, viewer: Member, team: Team, is_lead: bool, loads: list[Load],
             tz: dt.tzinfo) -> list[dict]:
    ld = next(x for x in loads if x.github == viewer.github)
    mine = dashboard.authored_by(snap, viewer.github)
    in_review = sum(1 for p in mine if p.requested and not p.draft)
    average = sum(x.reviews for x in loads) / len(loads)
    calendar = availability(ld, snap, tz)
    if is_lead and snap.lead_status:
        calendar = "🟢 No calls right now" if snap.lead_status == "No calls right now" else snap.lead_status
    blocks = [
        _header("📊 Your load"),
        _fields(
            f"*Reviews on you*\n{load_bar(ld.reviews)}  *{ld.reviews}*",
            f"*Calendar*\n{calendar}",
            f"*Your open PRs*\n*{ld.authored}*" + (f" · {in_review} waiting for review" if in_review else ""),
            f"*Oldest wait on you*\n{duration(ld.oldest_wait) if ld.oldest_wait else 'nothing waiting'}",
        ),
        _context(f"Team average {average:.1f} open reviews per person · bar fills at {FULL_LOAD}"),
    ]
    if mine:
        blocks.append(_section("*Your PRs*\n" + "\n".join(_my_pr_line(p, snap, team) for p in mine[:MAX_ROWS])))
    return blocks


def _handoffs(snap: Snapshot, viewer: Member, team: Team, is_lead: bool) -> list[dict]:
    items = snap.handoffs if is_lead else [h for h in snap.handoffs if h.to.lower() == viewer.github.lower()]
    title = "🤝 Your handoffs" if is_lead else f"🤝 Handed to you by {team.lead.name}"
    blocks = [_header(f"{title} · last 7 days")]
    if not items:
        return blocks + [_section("No Radar handoffs in the last 7 days.")]
    order = list(HANDOFF_STATE)
    squares = "".join(HANDOFF_STATE[h.state][0] for h in sorted(items, key=lambda h: order.index(h.state)))
    counts = " · ".join(f"{sum(1 for h in items if h.state == k)} {label}"
                        for k, (_, _, label) in HANDOFF_STATE.items() if any(h.state == k for h in items))
    lines = []
    for h in items[:MAX_ROWS]:
        _, icon, label = HANDOFF_STATE[h.state]
        who = f"→ *{team.display_name(h.to)}*" if is_lead else f"from {team.lead.name}"
        if not is_lead and h.state == "waiting":
            label = "waiting on your review"
        lines.append(f"{icon} <{h.url}|#{h.pr_number} {h.title}> {who} · {duration(snap.at - h.at)} ago · {label}")
    return blocks + [_section(f"{squares[:60]}  {counts}"), _section("\n".join(lines))]


def _team_load(snap: Snapshot, loads: list[Load], tz: dt.tzinfo) -> list[dict]:
    lines = []
    for ld in loads:
        oldest = f" · oldest {duration(ld.oldest_wait)}" if ld.oldest_wait else ""
        lines.append(f"{load_bar(ld.reviews)}  *{ld.name}* · {ld.reviews} review{_s(ld.reviews)}"
                     f" · {ld.authored} own PR{_s(ld.authored)} · {availability(ld, snap, tz)}{oldest}")
    return [_header("👥 Team load"), _section("\n".join(lines)),
            _context(f"Bar fills at {FULL_LOAD} open review requests · 🟩 1–2 · 🟨 3–4 · 🟥 5+")]


def _bottlenecks(snap: Snapshot, team: Team, loads: list[Load], stale_hours: float, tz: dt.tzinfo) -> list[dict]:
    lines = []
    old = dashboard.stale(snap, stale_hours, team.lead.github)
    if old:
        listed = ", ".join(f"<{p.url}|#{p.number}> on {team.display_name(login)} ({duration(w)})" for p, login, w in old[:5])
        lines.append(f"🔴 *{len(old)} review request{_s(len(old))}* older than {stale_hours:g}h: {listed}")
    total = sum(x.reviews for x in loads)
    top = loads[0]
    if total >= MIN_REQUESTS and top.reviews / total >= CONCENTRATION:
        lines.append(f"🧱 *{top.name}* holds {top.reviews} of {total} open review requests ({top.reviews / total:.0%})")
    for ld in loads:
        if ld.reviews and ld.busy_until:
            lines.append(f"📅 *{ld.name}* has {ld.reviews} review{_s(ld.reviews)} waiting but is "
                         f"{availability(ld, snap, tz).removeprefix('🔴 ')}")
    if not lines:
        lines.append("✅ No bottlenecks: nothing is stale and the load is spread.")
    return [_header("🚧 Bottlenecks"), _section("\n".join(lines))]


def _number(snap: Snapshot) -> list[dict]:
    period = "this week" if snap.stats_days == 7 else f"last {snap.stats_days} days"
    blocks = [_header(f"💰 The number · {period}")]
    st, sv = snap.stats, snap.savings
    if st is None or sv is None:
        return blocks + [_section("⚠️ Stats are unavailable right now. Try `/radar stats`.")]
    without, with_ = metrics.median(st.without_radar), metrics.median(st.with_radar)
    secs = lambda d: d.total_seconds() if d else 0  # noqa: E731
    scale = max(secs(without), secs(with_))
    if sv.prs:
        headline = metrics.savings_lines(sv, snap.stats_days)[0]
        if without and with_ and secs(with_) > 0 and with_ < without:
            headline += f"\n⚡ *{secs(without) / secs(with_):.1f}× faster* first review with Radar"
    else:
        headline = (f"📈 *No PR has been reviewed after a Radar action {period} yet.* Hours saved show up once a "
                    f"Radar handoff or review block gets its first review.")
    c = st.status_counts
    suggested = sum(c.values()) - c["superseded"]
    blocks += [
        _section(headline),
        _fields(
            f"*First review without Radar* (n={len(st.without_radar)})\n{bar(secs(without), scale, '🟥')}  *{metrics.fmt(without)}*",
            f"*First review with Radar* (n={len(st.with_radar)})\n{bar(secs(with_), scale, '🟩')}  *{metrics.fmt(with_)}*",
            f"*Still waiting*\n{len(st.still_waiting)} PR{_s(len(st.still_waiting))}",
            f"*Your time from card to decision*\n{metrics.fmt(metrics.median(st.decision_times))} median",
        ),
        _context(f"Radar decisions: {suggested} suggested · {c['handed_off'] + c['blocked']} approved "
                 f"({c['handed_off']} handoff{_s(c['handed_off'])}, {c['blocked']} block{_s(c['blocked'])}) · "
                 f"{c['kept']} kept · {c['snoozed']} snoozed · {c['pending']} open"),
    ]
    if snap.buy:
        blocks.append(_section(snap.buy))
    return blocks


def render(snap: Snapshot, viewer: Member, team: Team, tz: dt.tzinfo, stale_hours: float,
           links: dict[int, str] | None = None) -> dict:
    """links: card id -> Slack permalink of that card, for the lead's 'Open card' buttons."""
    is_lead = viewer.github.lower() == team.lead.github.lower()
    loads = dashboard.team_load(snap, team)
    buttons = [_button("🔄 Refresh", "home:refresh")]
    if is_lead:
        buttons.append(_button("🛰️ Sweep now", "home:sweep", style="primary"))
    blocks = [
        _header("🛰️ Review Radar"),
        _context(f"Hi *{viewer.name}* · {'engineering lead' if is_lead else 'developer'} · "
                 f"updated {snap.at.astimezone(tz):%H:%M}"),
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
    return {"type": "home", "blocks": [_header("🛰️ Review Radar"), _section(text)]}

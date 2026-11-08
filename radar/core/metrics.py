"""The number, computed: time to first review with vs without Radar, plus Radar's decision log. No I/O."""
from __future__ import annotations

import datetime as dt
import statistics
from collections import Counter
from dataclasses import dataclass

APPROVED = ("handed_off", "blocked")
# The lead's own clicks. "stale" is Radar skipping a PR that was reviewed or closed first, so it is not timed.
DECIDED = ("handed_off", "blocked", "kept", "snoozed")


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


MIN_BASELINE_PRS = 3  # fewer "without Radar" reviews than this: use the interview's baseline instead


@dataclass(frozen=True)
class Savings:
    prs: int  # PRs whose first review came after a Radar action
    baseline: dt.timedelta
    baseline_source: str
    wait_avoided: dt.timedelta
    factor: float
    dev_hours: float
    hourly_eur: float
    eur: float
    tokens: int
    usd_per_m_input: float
    usd_per_m_output: float
    model_usd: float


def savings(st: Stats, *, fallback_baseline_hours: float, blocked_cost_factor: float, hourly_eur: float,
            prompt_tokens: int, completion_tokens: int, usd_per_m_input: float, usd_per_m_output: float) -> Savings:
    """Developer-hours saved = sum over Radar-assisted PRs of max(0, baseline wait - actual wait) x blocked-cost factor.
    The baseline is the live "without Radar" median once there is enough data, else the interview's estimate."""
    if len(st.without_radar) >= MIN_BASELINE_PRS:
        baseline = median(st.without_radar)
        source = f"median without Radar, n={len(st.without_radar)}"
    else:
        baseline = dt.timedelta(hours=fallback_baseline_hours)
        source = ""  # placeholder baseline: not worth naming in the footnote
    avoided = sum((max(dt.timedelta(0), baseline - wait) for wait in st.with_radar), dt.timedelta(0))
    dev_hours = avoided.total_seconds() / 3600 * blocked_cost_factor
    model_usd = prompt_tokens / 1e6 * usd_per_m_input + completion_tokens / 1e6 * usd_per_m_output
    return Savings(len(st.with_radar), baseline, source, avoided, blocked_cost_factor, dev_hours, hourly_eur,
                   dev_hours * hourly_eur, prompt_tokens + completion_tokens, usd_per_m_input, usd_per_m_output, model_usd)


def _usd(value: float) -> str:
    """Cents normally; two significant digits for sub-cent amounts so real usage never shows as $0.000."""
    if value >= 0.01 or value == 0:
        return f"${value:.2f}"
    if value < 0.0001:
        return "<$0.0001"
    return f"${value:.2g}"


def decisions_summary(c: Counter) -> str:
    """'N suggested · … · K skipped · P open'; the parts add up to N (superseded rows moved to a newer card)."""
    suggested = sum(c.values()) - c["superseded"]
    approved = c["handed_off"] + c["blocked"]
    return (
        f"{suggested} suggested · {approved} approved "
        f"({c['handed_off']} handoff{'s' if c['handed_off'] != 1 else ''}, "
        f"{c['blocked']} review block{'s' if c['blocked'] != 1 else ''}) · {c['kept']} kept · {c['snoozed']} snoozed · "
        f"{c['stale']} skipped · {c['pending'] + c['working']} open"
    )


def savings_lines(sv: Savings, days: int, period: str | None = None) -> list[str]:
    if period:
        period = f"in {period}"
    else:
        period = "this week" if days == 7 else f"in the last {days} days"
    source = f" ({sv.baseline_source})" if sv.baseline_source else ""
    return [
        f"💰 *Saved ~{sv.dev_hours:.1f} developer-hours {period} ≈ €{sv.eur:,.0f}* vs ~{_usd(sv.model_usd)} model cost",
        f"_{sv.prs} PR{'s' if sv.prs != 1 else ''} reviewed after a Radar action · {fmt(sv.wait_avoided)} of waiting avoided "
        f"vs the {fmt(sv.baseline)} baseline{source} × {sv.factor:g} blocked-cost factor × €{sv.hourly_eur:g}/h · "
        f"{sv.tokens:,} LLM tokens at {_usd(sv.usd_per_m_input)}/{_usd(sv.usd_per_m_output)} per 1M in/out_",
    ]


def buy_line(tool: str, usd_per_seat: float, seats: int, model_usd: float, days: float, hosting_usd: float) -> str:
    """The decision in one line: a per-seat tool for everyone who opens PRs vs Radar's monthly cost
    (hosting + model usage projected from the stats window to a 30-day month)."""
    model_month = model_usd * 30 / days if days else model_usd
    return (
        f"🛒 *Buying instead:* {tool} ≈ ${usd_per_seat * seats:,.0f}/month ({seats} seat{'s' if seats != 1 else ''} × "
        f"${usd_per_seat:g}) vs Radar ≈ ${hosting_usd + model_month:,.2f}/month (hosting ${hosting_usd:g} + model "
        f"{_usd(model_month)}), and it doesn't reroute reviews around the lead's calls"
    )


def render(st: Stats, repo_name: str, days: int, sv: Savings | None = None, buy: str | None = None,
           period: str | None = None) -> str:
    """period: label for an explicit window (e.g. "08 Nov 14:00–17:30"); defaults to the last `days`."""
    def line(label: str, values: list[dt.timedelta]) -> str:
        return f"• {label}: *{fmt(median(values))}* median (n={len(values)})"

    lines = [
        f"*🛰️ Review Radar stats* · `{repo_name}` · {period or f'last {days} days'}",
        "*Time to first review* (opened → first review by someone other than the author, bots excluded)",
        line("Without Radar", st.without_radar),
        line("With Radar", st.with_radar),
    ]
    if st.still_waiting:
        longest = ", ".join(f"#{n} {fmt(w)}{' (Radar acted)' if acted else ''}" for n, w, acted in st.still_waiting[:3])
        lines.append(f"• Still waiting: {len(st.still_waiting)} PR{'s' if len(st.still_waiting) != 1 else ''} ({longest})")
    lines += [
        "*Radar decisions*",
        f"• {decisions_summary(st.status_counts)}",
        f"• Lead's time from card to decision: *{fmt(median(st.decision_times))}* median",
    ]
    if sv:
        lines += savings_lines(sv, days, period)
    if buy:
        lines.append(buy)
    return "\n".join(lines)

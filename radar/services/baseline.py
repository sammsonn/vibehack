"""Before number from a real repo's PR history: how long team PRs wait for a first review, and how concentrated
reviewing is on one person. Pure metrics plus a GitHub fetcher; works on any repo the token can read."""
from __future__ import annotations

import datetime as dt
import random
import statistics
from collections import Counter
from dataclasses import dataclass

from github import Auth, Github
from github.IssueEvent import IssueEvent
from github.PaginatedList import PaginatedList

from radar.core import rules

TEAM_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class PRHistory:
    number: int
    author: str
    start: dt.datetime  # opened, or last marked ready for review
    first_review_at: dt.datetime | None
    first_reviewer: str | None
    requested: frozenset[str]
    merged: bool
    open: bool
    files: tuple[str, ...] = ()
    changed_lines: int = 0


def is_bot(user) -> bool:
    return user is None or user.type == "Bot" or user.login.endswith("[bot]")


def is_team_pr(p) -> bool:
    # _rawData is the list response; p.raw_data would re-fetch every PR (one extra API call each)
    return not p.draft and not is_bot(p.user) and p._rawData.get("author_association") in TEAM_ASSOCIATIONS


def fetch(gh: Github, full_name: str, since: dt.datetime, with_files: bool = False) -> list[PRHistory]:
    """Team PRs (owner/member/collaborator authors, no bots, no drafts) opened since `since`.
    with_files: also fetch changed files (one extra request per PR), needed for the backtest."""
    repo = gh.get_repo(full_name)
    result = []
    for p in repo.get_pulls(state="all", sort="created", direction="desc"):
        if p.created_at < since:
            break
        if not is_team_pr(p):
            continue
        start, requested = p.created_at, set()
        for e in PaginatedList(IssueEvent, p._requester, f"{p.issue_url}/events", None):
            if e.event == "ready_for_review":
                start = e.created_at
            reviewer = getattr(e, "requested_reviewer", None)
            if e.event == "review_requested" and reviewer:
                requested.add(reviewer.login.lower())
        author = p.user.login.lower()
        reviews = [r for r in p.get_reviews()
                   if r.submitted_at and r.state != "PENDING" and not is_bot(r.user)
                   and r.user.login.lower() != author and r.submitted_at >= start]
        first = min(reviews, key=lambda r: r.submitted_at, default=None)
        files, changed = (), 0
        if with_files:
            changed_files = list(p.get_files())
            files = tuple(f.filename for f in changed_files)
            changed = sum(f.additions + f.deletions for f in changed_files)
        result.append(PRHistory(p.number, author, start, first.submitted_at if first else None,
                                first.user.login.lower() if first else None, frozenset(requested),
                                p.merged_at is not None, p.state == "open", files, changed))
    return result


def _median(values: list[dt.timedelta]) -> dt.timedelta | None:
    return statistics.median(values) if values else None


def _p75(values: list[dt.timedelta]) -> dt.timedelta | None:
    if len(values) < 4:
        return None
    return statistics.quantiles(values, n=4)[2]


@dataclass(frozen=True)
class Baseline:
    prs: int
    authors: int
    reviewed: int
    median: dt.timedelta | None
    p75: dt.timedelta | None
    merged_unreviewed: int
    merged: int
    waiting: int
    waiting_median: dt.timedelta | None
    top_reviewer: str | None
    top_share: float
    reviewers: int
    wait_top_requested: dt.timedelta | None
    wait_others_requested: dt.timedelta | None
    by_weekday: dict[str, tuple[dt.timedelta, int]]


def compute(prs: list[PRHistory], now: dt.datetime) -> Baseline:
    waits = {p.number: p.first_review_at - p.start for p in prs if p.first_review_at}
    first_reviewers = Counter(p.first_reviewer for p in prs if p.first_reviewer)
    top, top_n = first_reviewers.most_common(1)[0] if first_reviewers else (None, 0)
    on_top = [waits[p.number] for p in prs if p.number in waits and top in p.requested]
    on_others = [waits[p.number] for p in prs if p.number in waits and p.requested and top not in p.requested]
    day_waits: dict[str, list[dt.timedelta]] = {}
    for p in prs:
        if p.number in waits:
            day_waits.setdefault(WEEKDAYS[p.start.weekday()], []).append(waits[p.number])
    merged = [p for p in prs if p.merged]
    waiting = [now - p.start for p in prs if p.open and not p.first_review_at]
    return Baseline(
        prs=len(prs),
        authors=len({p.author for p in prs}),
        reviewed=len(waits),
        median=_median(list(waits.values())),
        p75=_p75(list(waits.values())),
        merged_unreviewed=sum(1 for p in merged if not p.first_review_at),
        merged=len(merged),
        waiting=len(waiting),
        waiting_median=_median(waiting),
        top_reviewer=top,
        top_share=top_n / sum(first_reviewers.values()) if first_reviewers else 0.0,
        reviewers=len(first_reviewers),
        wait_top_requested=_median(on_top),
        wait_others_requested=_median(on_others),
        by_weekday={d: (_median(v), len(v)) for d, v in sorted(day_waits.items(), key=lambda kv: WEEKDAYS.index(kv[0]))},
    )


def fmt(delta: dt.timedelta | None) -> str:
    if delta is None:
        return "n/a"
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours < 48 else f"{hours // 24}d {hours % 24}h"


def render(b: Baseline, full_name: str, days: int) -> str:
    lines = [
        f"Baseline: {full_name} - last {days} days - {b.prs} team PRs by {b.authors} authors",
        f"  Time to first review: median {fmt(b.median)}, p75 {fmt(b.p75)} (n={b.reviewed})",
        f"  Merged without any review: {b.merged_unreviewed} of {b.merged}",
        f"  Still waiting for a first review: {b.waiting} (median {fmt(b.waiting_median)} so far)",
    ]
    if b.top_reviewer:
        lines.append(f"  Review concentration: {b.top_reviewer} did {b.top_share:.0%} of first reviews "
                     f"({b.reviewers} people gave first reviews)")
    if b.wait_top_requested or b.wait_others_requested:
        lines.append(f"  Wait when {b.top_reviewer} was requested: {fmt(b.wait_top_requested)} "
                     f"vs others requested: {fmt(b.wait_others_requested)}")
    if b.by_weekday:
        lines.append("  By weekday opened: " + ", ".join(f"{d} {fmt(m)} (n={n})" for d, (m, n) in b.by_weekday.items()))
    lines.append(f"Slide: \"In a real {b.authors}-person team ({full_name}, {days} days, n={b.reviewed}), PRs waited "
                 f"{fmt(b.median)} for a first review; {b.top_share:.0%} of first reviews came from one person.\"")
    return "\n".join(lines)


def report(token: str | None, full_name: str, days: int = 90, now: dt.datetime | None = None,
           project: bool = False, acceptance: float = 0.8) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    gh = Github(auth=Auth.Token(token), per_page=100) if token else Github(per_page=100)
    prs = fetch(gh, full_name, now - dt.timedelta(days=days), with_files=project)
    b = compute(prs, now)
    text = render(b, full_name, days)
    if project and b.top_reviewer:
        text += "\n\n" + render_projection(prs, b, acceptance)
    return text


@dataclass(frozen=True)
class Projection:
    acceptance: float
    runs: int
    reviewed: int
    on_bottleneck: int
    handed_off: int
    kept_risky: int
    no_one_else: int
    risk_reasons: Counter
    pool: int
    pool_median: dt.timedelta | None
    actual_median: dt.timedelta | None
    projected_median: dt.timedelta
    median_low: dt.timedelta
    median_high: dt.timedelta
    actual_p75: dt.timedelta | None
    projected_p75: dt.timedelta
    bottleneck_actual_median: dt.timedelta | None
    bottleneck_projected_median: dt.timedelta
    hours_removed: float
    hours_low: float
    hours_high: float


def others_waits(prs: list[PRHistory], bottleneck: str) -> list[dt.timedelta]:
    """How fast this team actually got a first review when someone other than the bottleneck was asked."""
    return [p.first_review_at - p.start for p in prs
            if p.first_review_at and p.requested and bottleneck not in p.requested]


def waited_on(p: PRHistory, bottleneck: str) -> bool:
    if p.author == bottleneck:
        return False
    return bottleneck in p.requested or (not p.requested and p.first_reviewer == bottleneck)


def _pct(values: list, q: float):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def project(prs: list[PRHistory], bottleneck: str, acceptance: float, pool: list[dt.timedelta],
            runs: int = 2000, seed: int = 7) -> Projection:
    """Replays Radar on real history. PRs that waited on the bottleneck and that Radar's rules call low risk are
    handed to a teammate with probability `acceptance`; a handed-off PR's wait is drawn from `pool` (how fast
    other people in this team really responded), so it can also get slower. High-risk PRs and everything else
    keep their real wait. Monte Carlo over `runs` with a fixed seed, so results are reproducible."""
    team = {p.author for p in prs} | {p.first_reviewer for p in prs if p.first_reviewer}
    rows = []  # (actual wait, handoff-eligible, waited on bottleneck)
    kept, alone, reasons = 0, 0, Counter()
    for p in prs:
        if not p.first_review_at:
            continue
        wait, eligible, on_bn = p.first_review_at - p.start, False, waited_on(p, bottleneck)
        if on_bn:
            risky = rules.high_risk_reasons(p.files, p.changed_lines)
            if risky:
                kept += 1
                reasons.update(r.split(" (")[0] for r in risky)
            elif not team - {p.author, bottleneck}:
                alone += 1
            else:
                eligible = True
        rows.append((wait, eligible, on_bn))

    rng = random.Random(seed)
    medians, p75s, bn_medians, removed = [], [], [], []
    for _ in range(runs):
        waits = [rng.choice(pool) if eligible and rng.random() < acceptance else wait
                 for wait, eligible, _on in rows]
        medians.append(_median(waits))
        p75s.append(_p75(waits))
        bn_medians.append(_median([w for w, (_a, _e, on_bn) in zip(waits, rows) if on_bn]))
        removed.append(sum((a - w for (a, _e, _o), w in zip(rows, waits)), dt.timedelta(0)).total_seconds() / 3600)
    actual = [w for w, _e, _o in rows]
    return Projection(
        acceptance, runs, len(rows), sum(1 for _w, _e, on_bn in rows if on_bn), sum(1 for _w, e, _o in rows if e),
        kept, alone, reasons, len(pool), _median(pool), _median(actual), _median(medians),
        _pct(medians, 0.1), _pct(medians, 0.9), _p75(actual), _median(p75s),
        _median([w for w, _e, on_bn in rows if on_bn]), _median(bn_medians),
        statistics.median(removed), _pct(removed, 0.1), _pct(removed, 0.9),
    )


SENSITIVITY = (1.0, 0.8, 0.5)


def render_projection(prs: list[PRHistory], b: Baseline, acceptance: float) -> str:
    pool = others_waits(prs, b.top_reviewer)
    if len(pool) < 5:
        return "Backtest: not enough reviews by other people to estimate a handoff wait."
    pj = project(prs, b.top_reviewer, acceptance, pool)
    risky = ", ".join(f"{k} {v}" for k, v in pj.risk_reasons.most_common()) or "none"
    lines = [
        f"Backtest (PROJECTED by replaying Radar's rules on these real PRs; {pj.runs} simulated runs), "
        f"lead accepts {acceptance:.0%} of handoffs",
        f"  Reviewed PRs: {pj.reviewed}; waited on {b.top_reviewer}: {pj.on_bottleneck}",
        f"  Radar would hand off {pj.handed_off} (low risk) and keep {pj.kept_risky} with {b.top_reviewer} "
        f"(high risk: {risky}); {pj.no_one_else} had no other reviewer",
        f"  Wait after a handoff: drawn from this team's {pj.pool} real reviews by someone else "
        f"(median {fmt(pj.pool_median)}); can be slower than the real wait",
        f"  Median time to first review: {fmt(pj.actual_median)} actual -> {fmt(pj.projected_median)} projected "
        f"(80% range {fmt(pj.median_low)}-{fmt(pj.median_high)})",
        f"  p75: {fmt(pj.actual_p75)} actual -> {fmt(pj.projected_p75)} projected",
        f"  PRs that waited on {b.top_reviewer}: {fmt(pj.bottleneck_actual_median)} -> "
        f"{fmt(pj.bottleneck_projected_median)} median",
        f"  Waiting removed: ~{pj.hours_removed:.0f} hours over the window (80% range {pj.hours_low:.0f}-{pj.hours_high:.0f})",
        "  Sensitivity (lead's acceptance -> projected median): " + ", ".join(
            f"{a:.0%} -> {fmt(project(prs, b.top_reviewer, a, pool).projected_median)}" for a in SENSITIVITY),
    ]
    return "\n".join(lines)

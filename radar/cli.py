"""Review Radar CLI. Run from the repo root:

  python -m radar run                       Slack bot + automatic checks (keep exactly one running)
  python -m radar sweep [--at 16:50]        Post a card now; clicks are handled by the running bot
  python -m radar sweep --dry-run           Print suggestions without posting or storing anything
  python -m radar stats [--days 30]         Time to first review with vs without Radar, plus decisions
  python -m radar stats --since 14:00 --until 17:30   Same, for one window (e.g. a pilot phase)
  python -m radar baseline owner/repo [--days 90]     Before number from a real repo's PR history (uses GITHUB_TOKEN)
  python -m radar remind [--at 19:05]       Send due reminders for unclicked cards now (demo)
  python -m radar digest [--at 18:35]       Send the "while you were in calls" digest now if one is due (demo)
  python -m radar health                    Exit 0 if the running bot checked in recently (Docker HEALTHCHECK)
  python -m radar gate                      Update the radar/approval merge-gate status on open PRs now
"""
import argparse
import datetime as dt
import logging
import os
import sys

from slack_sdk import WebClient

from radar import config, runtime
from radar.integrations import google_calendar
from radar.services import approval_gate, baseline, digest, reminders, stats, sweep
from radar.slack import blocks, bot, messages
from radar.storage import store


def _print(ctx: sweep.LeadContext, suggestions, tz: dt.tzinfo, team: config.Team) -> None:
    print(ctx.header)
    for sg in suggestions:
        print(f"\n#{sg.pr.number} {sg.pr.title} ({team.display_name(sg.pr.author)}) [{sg.triage.risk}] {sg.triage.risk_reason}")
        print(f"  summary: {sg.triage.summary}")
        print(f"  look first: {sg.triage.look_first}")
        label = blocks.primary_label(sg, tz) or "(no action available)"
        print(f"  -> {sg.action}: {sg.why} | button: {label}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="radar", description="Review Radar")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="start the Slack bot and automatic checks")
    sw = sub.add_parser("sweep", help="post a Radar card now")
    sw.add_argument("--at", help="pretend it is this time (HH:MM today, or ISO timestamp)")
    sw.add_argument("--dry-run", action="store_true", help="print suggestions only")
    st = sub.add_parser("stats", help="time to first review, with vs without Radar")
    st.add_argument("--days", type=int, default=30)
    st.add_argument("--since", help="window start (HH:MM today, or ISO timestamp); overrides --days")
    st.add_argument("--until", help="window end (HH:MM today, or ISO timestamp); default now")
    bl = sub.add_parser("baseline", help="before number from a real repo's PR history")
    bl.add_argument("repo", help="owner/repo")
    bl.add_argument("--days", type=int, default=90)
    bl.add_argument("--project", action="store_true", help="backtest: replay Radar's rules on the real PRs")
    bl.add_argument("--acceptance", type=float, default=0.8, help="share of handoffs the lead accepts (backtest)")
    rm = sub.add_parser("remind", help="send due reminders for unclicked cards")
    rm.add_argument("--at", help="pretend it is this time (HH:MM today, or ISO timestamp)")
    dg = sub.add_parser("digest", help="send the post-call digest if one is due")
    dg.add_argument("--at", help="pretend it is this time (HH:MM today, or ISO timestamp)")
    sub.add_parser("gate", help="update the radar/approval status on open PRs")
    sub.add_parser("health", help="exit 0 if the running bot checked in recently")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to cp1252 and choke on emoji
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.command == "health":  # needs no secrets, so it works in a bare HEALTHCHECK
        age = runtime.heartbeat_age(runtime.heartbeat_path())
        limit = runtime.healthy_within(int(os.getenv("POLL_MINUTES", "5")))
        print("no heartbeat yet" if age is None else f"last check-in {age:.0f}s ago (limit {limit:.0f}s)")
        sys.exit(0 if age is not None and age <= limit else 1)

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "googleapiclient.discovery_cache"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        s = config.load()
    except config.ConfigError as e:
        sys.exit(f"Config error: {e}")

    if args.command == "run":
        try:
            bot.run(s)
        except (runtime.AlreadyRunning, store.NotWritable) as e:
            sys.exit(str(e))
        return
    if args.command == "stats":
        if args.since or args.until:
            tz = google_calendar.timezone_of(s.team.lead.email)
            since = bot.parse_at(args.since, tz) or dt.datetime.now(tz) - dt.timedelta(days=args.days)
            print(stats.report(s, args.days, since=since, until=bot.parse_at(args.until, tz)))
        else:
            print(stats.report(s, args.days))
        return
    if args.command == "gate":
        store.init()
        approval_gate.sync(s)
        return
    if args.command == "baseline":
        print(baseline.report(s.github_token, args.repo, args.days, project=args.project, acceptance=args.acceptance))
        return

    tz = google_calendar.timezone_of(s.team.lead.email)
    now = bot.parse_at(args.at, tz) or dt.datetime.now(dt.timezone.utc)
    store.init()
    if getattr(args, "dry_run", False):
        ctx, suggestions = sweep.build(s, now)
        _print(ctx, suggestions, tz, s.team)
        return
    client = WebClient(token=s.slack_bot_token)
    if args.command == "remind":
        due = reminders.due(s, now)
        for cid, pending in due:
            messages.post_reminder(client, s, cid, pending, now)
        print(f"Reminded {len(due)} card(s)" if due else "No reminders due.")
        return
    if args.command == "digest":
        result = digest.due(s, now)
        if result:
            messages.post_digest(client, s, *result)
        print(f"Sent digest with {len(result[1])} PR(s)" if result else "No digest due.")
        return
    card_id = sweep.run(s, lambda cid: messages.post_card(client, s, cid), now=now, trigger="manual")
    print(f"Posted card {card_id}" if card_id else "Nothing is waiting on the lead right now.")


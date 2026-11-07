"""Review Radar CLI. Run from the repo root:

  python -m radar run                       Slack bot + automatic checks (keep exactly one running)
  python -m radar sweep [--at 16:50]        Post a card now; clicks are handled by the running bot
  python -m radar sweep --dry-run           Print suggestions without posting or storing anything
  python -m radar stats [--days 30]         Time to first review with vs without Radar, plus decisions
  python -m radar remind [--at 19:05]       Send due reminders for unclicked cards now (demo)
"""
import argparse
import datetime as dt
import logging

from slack_sdk import WebClient

from radar import app, calendar_api, config, reminders, slack_ui, stats, store, sweep


def _print(ctx: sweep.LeadContext, suggestions, tz: dt.tzinfo, team: config.Team) -> None:
    print(ctx.header)
    for sg in suggestions:
        print(f"\n#{sg.pr.number} {sg.pr.title} ({team.display_name(sg.pr.author)}) [{sg.triage.risk}] {sg.triage.risk_reason}")
        print(f"  summary: {sg.triage.summary}")
        print(f"  look first: {sg.triage.look_first}")
        label = slack_ui.primary_label(sg, tz) or "(no action available)"
        print(f"  -> {sg.action}: {sg.why} | button: {label}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m radar", description="Review Radar")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="start the Slack bot and automatic checks")
    sw = sub.add_parser("sweep", help="post a Radar card now")
    sw.add_argument("--at", help="pretend it is this time (HH:MM today, or ISO timestamp)")
    sw.add_argument("--dry-run", action="store_true", help="print suggestions only")
    st = sub.add_parser("stats", help="time to first review, with vs without Radar")
    st.add_argument("--days", type=int, default=30)
    rm = sub.add_parser("remind", help="send due reminders for unclicked cards")
    rm.add_argument("--at", help="pretend it is this time (HH:MM today, or ISO timestamp)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "googleapiclient.discovery_cache"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    s = config.load()

    if args.command == "run":
        app.run(s)
        return
    if args.command == "stats":
        print(stats.report(s, args.days))
        return

    tz = calendar_api.timezone_of(s.team.lead.email)
    now = app.parse_at(args.at, tz) or dt.datetime.now(dt.timezone.utc)
    if args.dry_run:
        ctx, suggestions = sweep.build(s, now)
        _print(ctx, suggestions, tz, s.team)
        return
    store.init()
    client = WebClient(token=s.slack_bot_token)
    if args.command == "remind":
        due = reminders.due(s, now)
        for cid, pending in due:
            slack_ui.post_reminder(client, s, cid, pending, now)
        print(f"Reminded {len(due)} card(s)" if due else "No reminders due.")
        return
    card_id = sweep.run(s, lambda cid: slack_ui.post_card(client, s, cid), now=now, trigger="manual")
    print(f"Posted card {card_id}" if card_id else "Nothing is waiting on the lead right now.")


if __name__ == "__main__":
    main()

"""Slack bot (Socket Mode): handles card buttons, the /radar command and the automatic checks."""
import datetime as dt
import logging
import re
import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from radar import actions, calendar_api, slack_ui, stats, store, sweep
from radar.config import Settings

log = logging.getLogger(__name__)
USAGE = ("Usage: `/radar sweep [HH:MM]` (the optional time pretends it is that time today, for demos) "
         "or `/radar stats [days]`")


def parse_at(value: str | None, tz: dt.tzinfo) -> dt.datetime | None:
    """'16:50' means today at 16:50 in the lead's timezone; full ISO timestamps are accepted too."""
    if not value:
        return None
    if re.fullmatch(r"\d{1,2}:\d{2}", value):
        hour, minute = map(int, value.split(":"))
        return dt.datetime.now(tz).replace(hour=hour, minute=minute, second=0, microsecond=0)
    t = dt.datetime.fromisoformat(value)
    return t if t.tzinfo else t.replace(tzinfo=tz)


def create_app(s: Settings) -> App:
    app = App(token=s.slack_bot_token)
    lead_id = slack_ui.slack_id(app.client, s.team.lead.email)

    def post(card_id: int) -> None:
        slack_ui.post_card(app.client, s, card_id)

    @app.action(re.compile(r"^radar:"))
    def on_button(ack, body, action, client):
        ack()
        _, verb, raw_id = action["action_id"].split(":")
        user = body["user"]["id"]
        if user != lead_id:
            client.chat_postEphemeral(channel=body["channel"]["id"], user=user,
                                      text="Only the lead can approve Radar suggestions.")
            return
        if verb == "all":
            card_id = int(raw_id)
        else:
            row = store.suggestion(int(raw_id))
            if row is None:
                return
            card_id = row["card_id"]
        refresh = lambda: slack_ui.refresh_card(client, s, card_id)  # noqa: E731
        log.info("click: %s on %s by %s", verb, raw_id, user)
        if verb == "all":
            actions.approve_all(s, card_id, user, on_claimed=refresh)
        else:
            actions.execute(s, int(raw_id), verb, user, on_claimed=refresh)
        refresh()

    @app.command("/radar")
    def on_command(ack, command, respond):
        args = (command.get("text") or "").split()
        if args and args[0] == "stats":
            ack()
            try:
                days = int(args[1]) if len(args) > 1 else 30
                respond(response_type="in_channel", text=stats.report(s, days))
            except Exception as e:
                log.exception("stats failed")
                respond(f"⚠️ Stats failed: {e}")
            return
        if args and args[0] != "sweep":
            ack(USAGE)
            return
        ack("🛰️ Sweeping…")
        try:
            at = parse_at(args[1] if len(args) > 1 else None, calendar_api.timezone_of(s.team.lead.email))
            if sweep.run(s, post, now=at, trigger="manual") is None:
                respond("Nothing is waiting on the lead right now.")
        except Exception as e:
            log.exception("manual sweep failed")
            respond(f"⚠️ Sweep failed: {e}")

    return app


def _scheduler(s: Settings, post, remind, stop: threading.Event) -> None:
    interval = s.poll_minutes * 60
    while True:
        try:
            sweep.tick(s, post, remind=remind)
        except Exception:
            log.exception("automatic check failed")
        if stop.wait(interval):
            return


def run(s: Settings) -> None:
    store.init()
    app = create_app(s)
    stop = threading.Event()
    post = lambda card_id: slack_ui.post_card(app.client, s, card_id)  # noqa: E731
    remind = lambda card_id, pending: slack_ui.post_reminder(app.client, s, card_id, pending)  # noqa: E731
    threading.Thread(target=_scheduler, args=(s, post, remind, stop), daemon=True, name="radar-scheduler").start()
    log.info("Review Radar running: checking every %d min, pre-call window %d min", s.poll_minutes, s.precall_minutes)
    try:
        SocketModeHandler(app, s.slack_app_token).start()
    finally:
        stop.set()

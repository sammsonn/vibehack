"""Slack bot (Socket Mode): handles card buttons, the /radar command and the automatic checks."""
import datetime as dt
import logging
import re
import signal
import sys
import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from radar import runtime
from radar.config import Settings
from radar.integrations import google_calendar
from radar.services import actions, approval_gate, review_alerts, stats, sweep
from radar.slack import messages
from radar.storage import store

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
    lead_id = messages.slack_id(app.client, s.team.lead.email)

    def post(card_id: int) -> None:
        messages.post_card(app.client, s, card_id)

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
        refresh = lambda: messages.refresh_card(client, s, card_id)  # noqa: E731
        notify = lambda sg, login: messages.notify_reviewer(client, s, sg, login)  # noqa: E731
        picked = (action.get("selected_option") or {}).get("value")
        log.info("click: %s on %s by %s%s", verb, raw_id, user, f" (picked {picked})" if picked else "")
        if verb == "all":
            actions.approve_all(s, card_id, user, on_claimed=refresh, on_handoff=notify)
        else:
            actions.execute(s, int(raw_id), verb, user, on_claimed=refresh, reviewer=picked, on_handoff=notify)
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
            at = parse_at(args[1] if len(args) > 1 else None, google_calendar.timezone_of(s.team.lead.email))
            if sweep.run(s, post, now=at, trigger="manual") is None:
                respond("Nothing is waiting on the lead right now.")
        except Exception as e:
            log.exception("manual sweep failed")
            respond(f"⚠️ Sweep failed: {e}")

    return app


def _scheduler(s: Settings, post, remind, send_digest, stop: threading.Event) -> None:
    interval = s.poll_minutes * 60
    while True:
        try:
            sweep.tick(s, post, remind=remind, send_digest=send_digest)
        except Exception:
            log.exception("automatic check failed")
        runtime.beat(runtime.heartbeat_path())  # the loop is alive even if one check failed
        if stop.wait(interval):
            return


def run(s: Settings) -> None:
    with runtime.single_instance(runtime.lock_path()):
        _run(s)


def _run(s: Settings) -> None:
    store.init()
    runtime.beat(runtime.heartbeat_path())
    # `docker stop` sends SIGTERM: exit through the finally block instead of being killed mid-write.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    app = create_app(s)
    stop = threading.Event()
    post = lambda card_id: messages.post_card(app.client, s, card_id)  # noqa: E731
    remind = lambda card_id, pending: messages.post_reminder(app.client, s, card_id, pending)  # noqa: E731
    send_digest = lambda stretch, lines: messages.post_digest(app.client, s, stretch, lines)  # noqa: E731
    alert = lambda sg, login, state, unblocked: messages.post_review_alert(app.client, s, sg, login, state, unblocked)  # noqa: E731
    on_reviews = lambda pr, reviews: review_alerts.check(s, pr, reviews, alert)  # noqa: E731
    threading.Thread(target=_scheduler, args=(s, post, remind, send_digest, stop), daemon=True,
                     name="radar-scheduler").start()
    threading.Thread(target=approval_gate.loop, args=(s, stop, on_reviews), daemon=True, name="radar-gate").start()
    log.info("Review Radar running: checking every %d min, pre-call window %d min", s.poll_minutes, s.precall_minutes)
    handler = SocketModeHandler(app, s.slack_app_token)
    try:
        handler.start()
    finally:
        log.info("Review Radar shutting down")
        stop.set()
        handler.close()

"""Sends a sample Radar card by DM and logs button clicks. Usage: python scripts/slack_smoke_test.py you@email.com"""
import os
import sys

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()
app = App(token=os.environ["SLACK_BOT_TOKEN"])


def card_blocks() -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Review Radar test*: Client X demo in 10 min. 1 PR is waiting on you."}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "🟢 *#1 Fix typo in README*\nLow risk · Suggest a free teammate"}},
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ Hand off"}, "style": "primary", "action_id": "handoff", "value": "pr-1"},
                {"type": "button", "text": {"type": "plain_text", "text": "🙋 Keep"}, "action_id": "keep", "value": "pr-1"},
            ],
        },
    ]


@app.action("handoff")
@app.action("keep")
def on_click(ack, body, client):
    ack()
    action = body["actions"][0]
    print(f"CLICK: {action['action_id']} on {action['value']} by {body['user']['id']}", flush=True)
    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text=f"Test passed: you clicked {action['action_id']}.",
        blocks=[],
    )


if __name__ == "__main__":
    user_id = app.client.users_lookupByEmail(email=sys.argv[1])["user"]["id"]
    dm = app.client.conversations_open(users=user_id)["channel"]["id"]
    app.client.chat_postMessage(channel=dm, text="Review Radar test card", blocks=card_blocks())
    print("CARD SENT, waiting for clicks...", flush=True)
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()

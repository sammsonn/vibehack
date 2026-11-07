"""Verifies config and every integration. Run from vibehack/: python -m scripts.check_setup"""
import datetime as dt

from googleapiclient.discovery import build
from openai import OpenAI
from slack_sdk import WebClient

from radar import config
from radar.integrations import github
from radar.integrations.google_auth import get_credentials


def main() -> None:
    s = config.load()
    print(f"CONFIG ok: lead={s.team.lead.name} ({s.team.lead.github}), teammates={[m.name for m in s.team.teammates]}")

    repo = github.live_repo(s)
    print(f"GITHUB APP: access to {repo.full_name}, open PRs={repo.get_pulls(state='open').totalCount}")
    collaborators = {c.login.lower() for c in repo.get_collaborators()}
    for m in s.team.members:
        print(f"GITHUB {m.github}: {'collaborator' if m.github.lower() in collaborators else 'NOT A COLLABORATOR'}")

    slack = WebClient(token=s.slack_bot_token)
    for m in s.team.members:
        try:
            print(f"SLACK {m.email}: {slack.users_lookupByEmail(email=m.email)['user']['id']}")
        except Exception as e:
            print(f"SLACK {m.email}: NOT FOUND ({e})")

    cal = build("calendar", "v3", credentials=get_credentials())
    who = cal.calendars().get(calendarId="primary").execute()["id"].lower()
    print(f"CALENDAR logged in as {who}: {'bot account' if who == s.google_bot_email else 'NOT THE BOT ACCOUNT'}")
    try:
        cal.events().list(calendarId=s.team.lead.email, maxResults=1).execute()
        print(f"CALENDAR lead {s.team.lead.email}: readable")
    except Exception as e:
        print(f"CALENDAR lead {s.team.lead.email}: NOT READABLE ({e})")
    now = dt.datetime.now(dt.timezone.utc)
    fb = cal.freebusy().query(body={
        "timeMin": now.isoformat(),
        "timeMax": (now + dt.timedelta(hours=1)).isoformat(),
        "items": [{"id": m.email} for m in s.team.teammates],
    }).execute()["calendars"]
    for email, info in fb.items():
        print(f"FREEBUSY {email}: {'ERROR ' + info['errors'][0]['reason'] if info.get('errors') else 'ok'}")

    llm = OpenAI(base_url=s.llm_base_url, api_key=s.llm_api_key)
    reply = llm.chat.completions.create(model=s.llm_model, messages=[{"role": "user", "content": "Reply with OK"}], max_tokens=300)
    content = (reply.choices[0].message.content or "").strip()
    print(f"LLM {s.llm_model}: {content or 'EMPTY REPLY'}")


if __name__ == "__main__":
    main()

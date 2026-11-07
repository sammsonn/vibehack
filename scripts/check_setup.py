"""Verifies config and every integration. Run from vibehack/: python -m scripts.check_setup"""
from github import Auth, Github
from googleapiclient.discovery import build
from openai import OpenAI
from slack_sdk import WebClient

from radar import config
from scripts.google_login import get_credentials


def main() -> None:
    s = config.load()
    print(f"CONFIG ok: lead={s.team.lead.name} ({s.team.lead.github}), teammates={[m.name for m in s.team.teammates]}")

    repo = Github(auth=Auth.Token(s.github_token)).get_repo(s.live_repo)
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
    try:
        cal.events().list(calendarId=s.team.lead.email, maxResults=1).execute()
        print(f"CALENDAR lead {s.team.lead.email}: readable")
    except Exception as e:
        print(f"CALENDAR lead {s.team.lead.email}: NOT READABLE ({e})")

    llm = OpenAI(base_url=s.llm_base_url, api_key=s.llm_api_key)
    reply = llm.chat.completions.create(model=s.llm_model, messages=[{"role": "user", "content": "Reply with OK"}], max_tokens=5)
    print(f"LLM {s.llm_model}: {reply.choices[0].message.content.strip()}")


if __name__ == "__main__":
    main()

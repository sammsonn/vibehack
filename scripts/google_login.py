"""One-time Google login for the bot account. Run from vibehack/: python -m scripts.google_login"""
import datetime as dt
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from radar import config

ROOT = Path(__file__).resolve().parent.parent
SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]


def get_credentials() -> Credentials:
    token_path = ROOT / "token.json"
    creds = Credentials.from_authorized_user_file(token_path, SCOPES) if token_path.exists() else None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file(ROOT / "credentials.json", SCOPES)
        creds = flow.run_local_server(port=0, prompt="select_account")
    token_path.write_text(creds.to_json())
    return creds


if __name__ == "__main__":
    team = config.load().team
    lead = team.lead.email
    others = [m.email for m in team.teammates]

    service = build("calendar", "v3", credentials=get_credentials())
    primary = service.calendars().get(calendarId="primary").execute()
    print(f"LOGGED IN AS: {primary['id']}")

    now = dt.datetime.now(dt.timezone.utc)
    end = now + dt.timedelta(hours=24)
    events = service.events().list(
        calendarId=lead,
        timeMin=now.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute().get("items", [])
    print(f"LEAD {lead} EVENTS NEXT 24H: {len(events)}")
    for e in events:
        print(f"  {e['start'].get('dateTime', e['start'].get('date'))}  {e.get('summary', '(no title)')}")

    fb = service.freebusy().query(body={
        "timeMin": now.isoformat(),
        "timeMax": end.isoformat(),
        "items": [{"id": c} for c in others],
    }).execute()["calendars"]
    for cal, info in fb.items():
        status = f"ERROR {info['errors'][0]['reason']}" if info.get("errors") else f"{len(info['busy'])} busy blocks"
        print(f"FREEBUSY {cal}: {status}")

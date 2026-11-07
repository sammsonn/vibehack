from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from radar.config import GOOGLE_CREDENTIALS_PATH, GOOGLE_TOKEN_PATH

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]
TOKEN_PATH = GOOGLE_TOKEN_PATH


def get_credentials(allow_browser: bool = False) -> Credentials:
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES) if TOKEN_PATH.exists() else None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif allow_browser:
        flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDENTIALS_PATH, SCOPES)
        creds = flow.run_local_server(port=0, prompt="select_account")
    else:
        raise RuntimeError("No Google login found. Run: python -m scripts.google_login")
    TOKEN_PATH.write_text(creds.to_json())
    return creds

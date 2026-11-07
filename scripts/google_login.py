"""One-time Google login as the bot account. Run from vibehack/: python -m scripts.google_login"""
from radar.integrations.google_auth import TOKEN_PATH, get_credentials

if __name__ == "__main__":
    TOKEN_PATH.unlink(missing_ok=True)
    get_credentials(allow_browser=True)
    print("Saved token.json. Verify with: python -m scripts.check_setup")

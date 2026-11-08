import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# RADAR_HOME: where .env, team.json and the other files live (default: the repo checkout).
ROOT = Path(os.getenv("RADAR_HOME") or Path(__file__).resolve().parent.parent)
# Loaded at import so the file paths below can come from .env too. Real environment variables win.
load_dotenv(ROOT / ".env")


def path_setting(name: str, default: str) -> Path:
    """A file location from the environment; relative paths are resolved against ROOT."""
    return ROOT / (os.getenv(name) or default)


TEAM_FILE = path_setting("RADAR_TEAM_FILE", "team.json")
DB_PATH = path_setting("RADAR_DB_PATH", "radar.db")
GOOGLE_TOKEN_PATH = path_setting("GOOGLE_TOKEN_PATH", "token.json")
GOOGLE_CREDENTIALS_PATH = path_setting("GOOGLE_CREDENTIALS_PATH", "credentials.json")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Member:
    name: str
    github: str
    email: str


@dataclass(frozen=True)
class Team:
    lead: Member
    members: tuple[Member, ...]

    @property
    def teammates(self) -> tuple[Member, ...]:
        return tuple(m for m in self.members if m.github != self.lead.github)

    def by_github(self, login: str) -> Member | None:
        return next((m for m in self.members if m.github.lower() == login.lower()), None)

    def display_name(self, login: str) -> str:
        """Team name for a GitHub login; plain login (no @, so Slack won't mention anyone) otherwise."""
        member = self.by_github(login)
        return member.name if member else login


@dataclass(frozen=True)
class Settings:
    github_app_id: int
    github_app_key: str
    live_repo: str
    github_token: str | None
    before_repo: str | None
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    slack_bot_token: str
    slack_app_token: str
    google_bot_email: str
    stale_hours: float
    precall_minutes: int
    poll_minutes: int
    team: Team
    # Assumptions behind "hours saved" and model cost in /radar stats
    baseline_wait_hours: float = 8.0
    blocked_cost_factor: float = 0.25
    dev_hourly_eur: float = 20.0
    llm_usd_per_m_input: float = 0.10
    llm_usd_per_m_output: float = 0.50
    # The "buy" option in the decision line of /radar stats
    buy_tool_name: str = "CodeRabbit Team"
    buy_tool_usd_per_seat: float = 48.0
    hosting_usd_per_month: float = 5.0


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing {name} in .env (see .env.example)")
    return value


def _load_team(path: Path) -> Team:
    if not path.exists():
        raise ConfigError(f"Missing {path.name} (copy team.example.json)")
    data = json.loads(path.read_text(encoding="utf-8"))
    members = tuple(Member(m["name"], m["github"], m["email"].lower()) for m in data["members"])
    if len({m.github.lower() for m in members}) != len(members):
        raise ConfigError("Duplicate github username in team.json")
    lead = next((m for m in members if m.github.lower() == data["lead"].lower()), None)
    if lead is None:
        raise ConfigError(f"Lead '{data['lead']}' is not listed in team.json members")
    if len(members) < 2:
        raise ConfigError("team.json needs the lead plus at least one teammate")
    return Team(lead=lead, members=members)


def load() -> Settings:
    key_path = path_setting("GITHUB_APP_KEY_PATH", "github-app.pem")
    if not key_path.exists():
        raise ConfigError(f"Missing GitHub App private key at {key_path}")
    return Settings(
        github_app_id=int(_require("GITHUB_APP_ID")),
        github_app_key=key_path.read_text(encoding="utf-8"),
        live_repo=_require("LIVE_REPO"),
        github_token=(os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN_1") or "").strip() or None,
        before_repo=os.getenv("BEFORE_REPO", "").strip() or None,
        llm_base_url=_require("LLM_BASE_URL"),
        llm_api_key=_require("LLM_API_KEY"),
        llm_model=_require("LLM_MODEL"),
        slack_bot_token=_require("SLACK_BOT_TOKEN"),
        slack_app_token=_require("SLACK_APP_TOKEN"),
        google_bot_email=_require("GOOGLE_BOT_EMAIL").lower(),
        stale_hours=float(os.getenv("STALE_HOURS", "4")),
        precall_minutes=int(os.getenv("PRECALL_MINUTES", "10")),
        poll_minutes=int(os.getenv("POLL_MINUTES", "5")),
        baseline_wait_hours=float(os.getenv("BASELINE_WAIT_HOURS", "8")),
        blocked_cost_factor=float(os.getenv("BLOCKED_COST_FACTOR", "0.25")),
        dev_hourly_eur=float(os.getenv("DEV_HOURLY_EUR", "20")),
        llm_usd_per_m_input=float(os.getenv("LLM_USD_PER_M_INPUT", "0.10")),
        llm_usd_per_m_output=float(os.getenv("LLM_USD_PER_M_OUTPUT", "0.50")),
        buy_tool_name=os.getenv("BUY_TOOL_NAME", "CodeRabbit Team").strip() or "CodeRabbit Team",
        buy_tool_usd_per_seat=float(os.getenv("BUY_TOOL_USD_PER_SEAT", "48")),
        hosting_usd_per_month=float(os.getenv("RADAR_HOSTING_USD_PER_MONTH", "5")),
        team=_load_team(TEAM_FILE),
    )

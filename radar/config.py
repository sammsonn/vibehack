import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


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
    load_dotenv(ROOT / ".env")
    key_path = ROOT / os.getenv("GITHUB_APP_KEY_PATH", "github-app.pem")
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
        team=_load_team(ROOT / "team.json"),
    )

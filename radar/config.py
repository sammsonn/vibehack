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


@dataclass(frozen=True)
class Settings:
    github_token: str
    live_repo: str
    before_repo: str | None
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    slack_bot_token: str
    slack_app_token: str
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
    return Settings(
        github_token=_require("GITHUB_TOKEN"),
        live_repo=_require("LIVE_REPO"),
        before_repo=os.getenv("BEFORE_REPO", "").strip() or None,
        llm_base_url=_require("LLM_BASE_URL"),
        llm_api_key=_require("LLM_API_KEY"),
        llm_model=_require("LLM_MODEL"),
        slack_bot_token=_require("SLACK_BOT_TOKEN"),
        slack_app_token=_require("SLACK_APP_TOKEN"),
        stale_hours=float(os.getenv("STALE_HOURS", "4")),
        precall_minutes=int(os.getenv("PRECALL_MINUTES", "10")),
        poll_minutes=int(os.getenv("POLL_MINUTES", "5")),
        team=_load_team(ROOT / "team.json"),
    )

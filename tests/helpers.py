"""Shared fixtures: a small team and PRs at fixed times on the demo day."""
import datetime as dt
from zoneinfo import ZoneInfo

from radar.config import Member, Team
from radar.core.models import PR, Triage

TZ = ZoneInfo("Europe/Bucharest")
NOW = dt.datetime(2026, 11, 7, 16, 50, tzinfo=TZ)
LEAD = Member("Samson", "lead", "lead@x.com")
ANDREI = Member("Andrei", "andrei", "andrei@x.com")
CONE = Member("Cone", "cone", "cone@x.com")
TEAM = Team(lead=LEAD, members=(LEAD, ANDREI, CONE))
LOW = Triage("Fixes a typo.", "low", "Docs only", "README.md")
HIGH = Triage("Adds retries.", "high", "Touches payments", "payments/webhook.py")


def at(hour: int, minute: int = 0) -> dt.datetime:
    return NOW.replace(hour=hour, minute=minute)


def make_pr(number=1, author="andrei", files=("README.md",), changed=2) -> PR:
    return PR(number, f"PR {number}", f"https://github.com/o/r/pull/{number}", author, "abc",
              at(10), files, changed, 0, "", "")

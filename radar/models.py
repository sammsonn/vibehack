from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass(frozen=True)
class PR:
    number: int
    title: str
    url: str
    author: str
    head_sha: str
    waiting_since: datetime
    files: tuple[str, ...] = ()
    additions: int = 0
    deletions: int = 0
    body: str = ""
    diff: str = ""


@dataclass(frozen=True)
class Triage:
    summary: str
    risk: str  # "low" | "high"
    risk_reason: str
    look_first: str


@dataclass(frozen=True)
class Candidate:
    github: str
    name: str
    free: bool
    file_commits: int
    open_reviews: int
    busy_until: datetime | None = None

    @property
    def why(self) -> str:
        parts = ["free" if self.free else "busy"]
        if self.file_commits:
            parts.append(f"{self.file_commits} commit{'s' if self.file_commits != 1 else ''} to these files in 90 days")
        parts.append(f"{self.open_reviews} open review{'s' if self.open_reviews != 1 else ''}")
        return ", ".join(parts)


@dataclass(frozen=True)
class Suggestion:
    """What Radar proposes for one PR. action: "handoff" (to reviewer) or "block" (lead keeps it, slot reserved)."""
    pr: PR
    triage: Triage
    action: str
    why: str
    reviewer: Candidate | None = None
    slot: tuple[datetime, datetime] | None = None
    lead_busy_until: datetime | None = None
    candidates: tuple[Candidate, ...] = ()  # every eligible reviewer, best first (for the lead's own pick)

    def candidate(self, github: str) -> Candidate | None:
        return next((c for c in self.candidates if c.github.lower() == github.lower()), None)

    def to_json(self) -> str:
        data = asdict(self)
        data["pr"].pop("diff")
        data["pr"].pop("body")
        return json.dumps(data, default=lambda o: o.isoformat() if isinstance(o, datetime) else str(o))

    @classmethod
    def from_json(cls, raw: str) -> Suggestion:
        d = json.loads(raw)
        ts = lambda v: datetime.fromisoformat(v) if v else None  # noqa: E731
        pr = d["pr"]
        pr["waiting_since"] = ts(pr["waiting_since"])
        pr["files"] = tuple(pr["files"])
        def candidate(c: dict | None) -> Candidate | None:
            return Candidate(**{**c, "busy_until": ts(c.get("busy_until"))}) if c else None

        return cls(
            pr=PR(**pr),
            triage=Triage(**d["triage"]),
            action=d["action"],
            why=d["why"],
            reviewer=candidate(d.get("reviewer")),
            slot=(ts(d["slot"][0]), ts(d["slot"][1])) if d.get("slot") else None,
            lead_busy_until=ts(d.get("lead_busy_until")),
            candidates=tuple(candidate(c) for c in d.get("candidates", [])),
        )

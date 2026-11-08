"""Demo data for Review Radar: realistic PRs from the teammates and busy calendars, all removable again.

  python -m scripts.demo_data seed               PRs + calendars, demo starting now
  python -m scripts.demo_data seed --in 90       same, but the calendars fit a demo starting in 90 minutes
  python -m scripts.demo_data seed --only prs    just the PRs
  python -m scripts.demo_data seed --only calendar --in 30
  python -m scripts.demo_data clean              close every open PR, delete demo branches and every event Radar made
  python -m scripts.demo_data reset --in 90      clean, then seed (run it as often as you like)

PRs are opened with each teammate's own token (GITHUB_TOKEN, GITHUB_TOKEN_1..9 in .env), so they have real
authors. Demo branches start with `demo/`; demo events carry the private property radar_demo=1. `clean` closes
all open PRs on the test repo and deletes Radar's review blocks and demo events; other events are never touched.
"""
import argparse
import datetime as dt
import os
import sys

from github import Auth, Github, GithubException

from radar import config
from radar.integrations import google_calendar

BRANCH_PREFIX = "demo/"
DEMO_TAG = {"radar_demo": "1"}
MARKER = "<!-- radar-demo -->"

# author: index into the teammates that have a token; reviewers: "lead" and/or "next" (the following teammate)
PRS = [
    {
        "branch": "docs-export-columns", "author": 0, "reviewers": ["lead"],
        "title": "Document the CSV columns of the invoice export",
        "body": "Lists every column of the export with its type, so finance can map it in their sheet.",
        "files": {"README.md": ("append", "\n## Export columns\n| column | type |\n|---|---|\n| number | INV-YYYY-NNNNN |\n"
                                          "| issued_at | ISO date |\n| amount | cents, integer |\n| currency | ISO 4217 |\n")},
    },
    {
        "branch": "utils-normalize-whitespace", "author": 1, "reviewers": ["lead"],
        "title": "Collapse repeated whitespace in customer names",
        "body": "Customer names pasted from emails often contain double spaces and tabs; this normalises them.",
        "files": {"src/utils.py": ("append", "\n\ndef normalize_whitespace(text: str) -> str:\n"
                                             "    \"\"\"'Acme   SRL\\t' -> 'Acme SRL'.\"\"\"\n"
                                             "    return \" \".join(text.split())\n")},
    },
    {
        "branch": "webhook-secret-rotation", "author": 2, "reviewers": ["lead"],
        "title": "Accept the previous Stripe signing secret during rotation",
        "body": "While we rotate the webhook secret, events signed with the old secret are still accepted for 24h.",
        "files": {"payments/webhook.py": ("append", "\n\nimport hmac\nimport os\n\n\n"
                                                    "def signing_secrets() -> list[str]:\n"
                                                    "    \"\"\"Current secret first, then the previous one while it is rotated out.\"\"\"\n"
                                                    "    secrets = [os.environ[\"STRIPE_WEBHOOK_SECRET\"]]\n"
                                                    "    if os.getenv(\"STRIPE_WEBHOOK_SECRET_PREVIOUS\"):\n"
                                                    "        secrets.append(os.environ[\"STRIPE_WEBHOOK_SECRET_PREVIOUS\"])\n"
                                                    "    return secrets\n\n\n"
                                                    "def signature_matches(payload: bytes, signature: str) -> bool:\n"
                                                    "    return any(hmac.compare_digest(hmac.new(s.encode(), payload, \"sha256\").hexdigest(), signature)\n"
                                                    "               for s in signing_secrets())\n")},
    },
    {
        "branch": "migration-paid-at-index", "author": 0, "reviewers": ["lead"],
        "title": "Index invoices by paid_at for the monthly report",
        "body": "The monthly revenue report scans the whole invoices table; this adds the index it needs.",
        "files": {"migrations/0043_invoices_paid_at_index.sql": ("create", "CREATE INDEX CONCURRENTLY IF NOT EXISTS invoices_paid_at_idx\n"
                                                                          "    ON invoices (paid_at)\n    WHERE paid_at IS NOT NULL;\n")},
    },
    {
        "branch": "tests-invoice-number", "author": 1, "reviewers": ["lead", "next"],
        "title": "Test invoice_number edge cases",
        "body": "Covers the first sequence number, padding past 99999 and custom prefixes.",
        "files": {"tests/test_utils.py": ("append", "\n\ndef test_invoice_number_pads_and_prefixes():\n"
                                                    "    from src.utils import invoice_number\n"
                                                    "    assert invoice_number(1, 2026) == \"INV-2026-00001\"\n"
                                                    "    assert invoice_number(123456, 2026) == \"INV-2026-123456\"\n"
                                                    "    assert invoice_number(7, 2026, prefix=\"CN\") == \"CN-2026-00007\"\n")},
    },
    {
        "branch": "db-retry-jitter", "author": 2, "reviewers": ["lead"],
        "title": "Add jitter to payment DB write retries",
        "body": "Retries after a deadlock all fired at the same moment; random jitter spreads them out.",
        "files": {"payments/db.py": ("append", "\n\nimport random\n\n\n"
                                               "def backoff_seconds(attempt: int, base: float = 0.5) -> float:\n"
                                               "    \"\"\"Exponential backoff with full jitter.\"\"\"\n"
                                               "    return random.uniform(0, base * 2 ** attempt)\n")},
    },
    {
        "branch": "export-currency-column", "author": 0, "reviewers": ["next"],
        "title": "Add the currency column to the CSV export",
        "body": "Mixed-currency customers could not tell EUR from RON rows in the export.",
        "files": {"src/export.py": ("append", "\n\nEXPORT_COLUMNS = [\"number\", \"issued_at\", \"amount\", \"currency\"]\n")},
    },
]

# (who, title, start offset from the demo start in minutes, length in minutes). who: "lead" or a teammate index.
# The lead is in calls for the whole demo (so review blocks land after them); teammate 2 is busy, 0 and 1 are free.
EVENTS = [
    ("lead", "Client call: Acme onboarding", -10, 60),
    ("lead", "Client call: Globex renewal", 55, 45),
    ("lead", "Sales demo: Initech", 105, 45),
    ("lead", "Client call: Umbrella quarterly review", 24 * 60, 60),
    (2, "Dentist", -30, 150),
    (0, "Sprint retro", 120, 45),
    (1, "Pairing: export API", 180, 60),
]


def _tokens() -> dict[str, str]:
    """GitHub login (lowercase) -> token, from GITHUB_TOKEN and GITHUB_TOKEN_1..9."""
    result = {}
    for var in ["GITHUB_TOKEN"] + [f"GITHUB_TOKEN_{i}" for i in range(1, 10)]:
        token = (os.getenv(var) or "").strip()
        if token:
            login = Github(auth=Auth.Token(token)).get_user().login.lower()
            result.setdefault(login, token)
    return result


def _repo(s: config.Settings, token: str):
    return Github(auth=Auth.Token(token)).get_repo(s.live_repo)


def seed_prs(s: config.Settings) -> None:
    tokens = _tokens()
    authors = [m for m in s.team.teammates if m.github.lower() in tokens]
    if not authors:
        sys.exit("No teammate has a GitHub token in .env (GITHUB_TOKEN_1..9).")
    print(f"Authors with tokens: {', '.join(m.name for m in authors)}")
    for spec in PRS:
        author = authors[spec["author"] % len(authors)]
        repo = _repo(s, tokens[author.github.lower()])
        branch = BRANCH_PREFIX + spec["branch"]
        if any(True for _ in repo.get_pulls(state="open", head=f"{repo.owner.login}:{branch}")):
            print(f"  skip {branch}: already open")
            continue
        base = repo.get_branch(repo.default_branch).commit.sha
        try:
            repo.get_git_ref(f"heads/{branch}").delete()
        except GithubException:
            pass
        repo.create_git_ref(f"refs/heads/{branch}", base)
        for path, (mode, text) in spec["files"].items():
            message = spec["title"]
            if mode == "create":
                repo.create_file(path, message, text, branch=branch)
            else:
                current = repo.get_contents(path, ref=branch)
                repo.update_file(path, message, current.decoded_content.decode() + text, current.sha, branch=branch)
        pr = repo.create_pull(base=repo.default_branch, head=branch, title=spec["title"],
                              body=f"{spec['body']}\n\n{MARKER}")
        reviewers = []
        for r in spec["reviewers"]:
            if r == "lead":
                reviewers.append(s.team.lead.github)
            else:
                others = [m for m in s.team.teammates if m.github != author.github]
                reviewers.append(others[spec["author"] % len(others)].github)
        pr.create_review_request(reviewers=reviewers)
        names = ", ".join(s.team.display_name(r) for r in reviewers)
        print(f"  #{pr.number} {spec['title']} (by {author.name}, review: {names})")


def seed_calendar(s: config.Settings, start_at: dt.datetime) -> None:
    svc = google_calendar.service()
    tz = google_calendar.timezone_of(s.team.lead.email)
    print(f"  demo starts {start_at.astimezone(tz):%a %H:%M}")
    for who, title, offset, minutes in EVENTS:
        member = s.team.lead if who == "lead" else s.team.teammates[who % len(s.team.teammates)]
        start = start_at + dt.timedelta(minutes=offset)
        svc.events().insert(calendarId=member.email, sendUpdates="none", body={
            "summary": title,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": (start + dt.timedelta(minutes=minutes)).isoformat()},
            "extendedProperties": {"private": DEMO_TAG},
        }).execute()
        print(f"  {member.name}: {title} {start.astimezone(tz):%a %H:%M}–{(start + dt.timedelta(minutes=minutes)).astimezone(tz):%H:%M}")


def _radar_events(svc, calendar_id: str, now: dt.datetime) -> list[dict]:
    """Radar's review blocks and the demo events in one calendar, from 60 days back to 60 days ahead."""
    found: dict[str, dict] = {}
    for query in ({"privateExtendedProperty": "radar=review-block"}, {"privateExtendedProperty": "radar_demo=1"},
                  {"q": "Review PR"}):
        page = None
        while True:
            res = svc.events().list(calendarId=calendar_id, singleEvents=True, pageToken=page,
                                    timeMin=(now - dt.timedelta(days=60)).isoformat(),
                                    timeMax=(now + dt.timedelta(days=60)).isoformat(), **query).execute()
            for e in res.get("items", []):
                tags = e.get("extendedProperties", {}).get("private", {})
                # The text search also matches titles that merely mention "Review PR"; keep only Radar's own.
                if tags.get("radar") == "review-block" or tags.get("radar_demo") == "1" \
                        or e.get("summary", "").startswith(google_calendar.REVIEW_BLOCK_PREFIX):
                    found[e["id"]] = e
            page = res.get("nextPageToken")
            if not page:
                break
    return list(found.values())


def clean(s: config.Settings, now: dt.datetime) -> None:
    """Closes every open PR on the test repo, deletes the demo branches and every calendar event Radar made."""
    tokens = _tokens()
    if tokens:
        repo = _repo(s, next(iter(tokens.values())))
        for pr in list(repo.get_pulls(state="open")):
            pr.edit(state="closed")
            print(f"  closed #{pr.number} {pr.title}")
        for ref in repo.get_git_matching_refs(f"heads/{BRANCH_PREFIX}"):
            ref.delete()
            print(f"  deleted branch {ref.ref.removeprefix('refs/heads/')}")
    svc = google_calendar.service()
    for m in s.team.members:
        for e in _radar_events(svc, m.email, now):
            svc.events().delete(calendarId=m.email, eventId=e["id"], sendUpdates="none").execute()
            print(f"  {m.name}: deleted {e.get('summary')}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="demo_data", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["seed", "clean", "reset"])
    parser.add_argument("--only", choices=["prs", "calendar"])
    parser.add_argument("--in", dest="in_minutes", type=int, default=0, metavar="MINUTES",
                        help="the demo starts this many minutes from now (calendar events are placed around it)")
    args = parser.parse_args()
    s = config.load()
    now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    if args.command in ("clean", "reset"):
        print("Clean:")
        clean(s, now)
        if args.command == "clean":
            return
    if args.only in (None, "prs"):
        print("PRs:")
        seed_prs(s)
    if args.only in (None, "calendar"):
        print("Calendar:")
        seed_calendar(s, now + dt.timedelta(minutes=args.in_minutes))


if __name__ == "__main__":
    main()

# Review Radar

When the engineering lead switches to client calls, the code keeps moving. Ten minutes before each call, Radar
checks which pull requests are waiting on the lead, summarizes and risk-rates them, and proposes in Slack:

- **Low risk** → hand off to the best free teammate (free in Google Calendar, knows the files, fewest open reviews).
- **High risk** (auth, payments, migrations, infra, security, large diffs) → keep it with the lead and reserve the
  first free 30 min after the calls.

Nothing happens without the lead's click. On approval Radar requests the review on GitHub and posts the summary as a
PR comment, or creates the review block in the lead's calendar.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                 # fill in values
cp team.example.json team.json       # lead + teammates (GitHub login, Google/Slack email)
.venv/bin/python -m scripts.google_login   # once, log in as the Google bot account
.venv/bin/python -m scripts.check_setup    # verifies every integration
```

## Run

```bash
.venv/bin/python -m radar run                        # Slack bot + automatic checks (run exactly one copy)
.venv/bin/python -m radar sweep --at 16:50           # post a card now, as if it were 16:50 (demo)
.venv/bin/python -m radar sweep --at 16:50 --dry-run # print suggestions, post nothing
.venv/bin/python -m radar stats --days 30            # the number: time to first review, with vs without Radar
.venv/bin/python -m radar remind --at 19:05          # send due reminders for unclicked cards now (demo)
.venv/bin/python -m unittest discover tests          # unit tests
```

In Slack, `/radar sweep [HH:MM]` and `/radar stats [days]` do the same, once the `/radar` slash command is added in
the Slack app settings (Slash Commands → Create New Command, then reinstall the app).

**The number:** time to first review on the live repo, from when the PR was opened (or marked ready) to the first
review by someone other than the author, bots excluded. "With Radar" counts PRs where the lead approved a Radar
handoff or review block before that first review; all other PRs count as "without Radar".

Automatic triggers (every `POLL_MINUTES`): a card before each of the lead's calendar events (`PRECALL_MINUTES`), and
for PRs waiting longer than `STALE_HOURS` while the lead is in a meeting. When the lead is free again and a card
is still unclicked, Radar replies once in the card's thread (PRs reviewed in the meantime are skipped). Every card, suggestion and decision is
logged in `radar.db` (SQLite).

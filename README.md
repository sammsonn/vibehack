# Review Radar

When the engineering lead switches to client calls, the code keeps moving. Ten minutes before each call, Radar
checks which pull requests are waiting on the lead, summarizes and risk-rates them, and proposes in Slack:

- **Low risk** → hand off to the best free teammate (free in Google Calendar, knows the files, fewest open reviews).
- **High risk** (auth, payments, migrations, infra, security, large diffs) → keep it with the lead and reserve the
  first free 30 min after the calls.

Nothing happens without the lead's click. Each PR on the card also has a **Choose reviewer** menu listing every eligible engineer
in Radar's ranked order (with the reasons), so the lead can hand off to someone else, even on a high-risk PR. On approval Radar requests the review on GitHub and posts the summary as a
PR comment, or creates the review block in the lead's calendar.

## Code layout

Each layer only imports from the layers listed after it, so the core logic is testable without any network.

```
radar/
  cli.py                 `radar` command: run, sweep, stats, remind, gate, health
  slack/
    bot.py               Socket Mode bot: buttons, /radar command, scheduler + merge-gate threads
    messages.py          posts and updates cards and reminders
    blocks.py            Block Kit rendering (pure)
  services/              use cases, orchestrating integrations + storage
    sweep.py             which PRs wait on the lead, what to suggest, when to sweep automatically
    triage.py            risk = path rules first, the LLM can only raise it
    actions.py           every approved GitHub/Calendar write
    reminders.py         one nudge per unclicked card once the lead is free
    approval_gate.py     radar/approval commit status (merge gate)
    stats.py             the number, from GitHub history + the decision log
  storage/store.py       SQLite: cards, suggestions, decisions, versioned migrations
  integrations/          thin clients: github.py, google_calendar.py, google_auth.py, llm.py
  core/                  pure logic, no I/O: models, rules, ranking, schedule (busy/free math), metrics
  config.py              settings, team, file locations
  runtime.py             single-instance lock, heartbeat
tests/                   one file per layer; external services are mocked
```

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -e .   # pinned dependencies + the `radar` command
cp .env.example .env                 # fill in values
cp team.example.json team.json       # lead + teammates (GitHub login, Google/Slack email)
.venv/bin/python -m scripts.google_login   # once, log in as the Google bot account
.venv/bin/python -m scripts.check_setup    # verifies every integration
```

## Run

```bash
.venv/bin/radar run                        # Slack bot + automatic checks (only one per data directory, see below)
.venv/bin/radar sweep --at 16:50           # post a card now, as if it were 16:50 (demo)
.venv/bin/radar sweep --at 16:50 --dry-run # print suggestions, post nothing
.venv/bin/radar stats --days 30            # the number: time to first review, with vs without Radar
.venv/bin/radar remind --at 19:05          # send due reminders for unclicked cards now (demo)
.venv/bin/radar gate                       # update the radar/approval merge-gate status on open PRs
.venv/bin/radar health                     # exit 0 if the running bot checked in recently
.venv/bin/python -m unittest discover tests
```

`python -m radar …` works the same without installing the package.

## Deploy with Docker

The image holds only code and pinned dependencies. Secrets are mounted read-only at runtime, and state
(`radar.db`, the refreshed Google token, the lock and heartbeat) lives in the `radar-data` volume.

```bash
# Needs .env, team.json, github-app.pem, credentials.json and token.json in the repo folder (see Setup).
docker compose up -d --build
docker compose logs -f radar
docker compose ps                          # shows "healthy" once the first check has run
docker compose run --rm radar python -m radar sweep --dry-run   # one-off commands use the same config
```

- **One bot only.** `radar run` holds a lock next to `radar.db` and refuses to start a second copy on the same data
  directory. It cannot see bots on other machines: if two people run Radar against the same Slack app, button
  clicks are split between them. Stop the local bot before starting the container.
- **Health check:** the scheduler writes a heartbeat after every check; `radar health` fails if none arrived within
  two check intervals plus 2 minutes.
- **Shutdown:** `docker compose stop` sends SIGTERM; Radar closes the Slack connection and stops its loops cleanly.
- **Secrets on a shared host:** keep `github-app.pem`, `token.json` and `.env` readable only by you
  (`chmod 600`). The container user (uid 10001) must still be able to read the mounted files, so on shared hosts
  prefer Docker secrets or a host directory owned by that uid.
- **File locations** can be changed with `RADAR_TEAM_FILE`, `RADAR_DB_PATH`, `GOOGLE_TOKEN_PATH`,
  `GOOGLE_CREDENTIALS_PATH` and `GITHUB_APP_KEY_PATH` (relative paths resolve against the repo, or `RADAR_HOME`).
- **Database schema** is versioned (`PRAGMA user_version`) and migrated automatically on start.
- **Dependencies:** direct ones with ranges in `pyproject.toml`; exact tested versions in `requirements.txt`.

## Slack and the number

In Slack, `/radar sweep [HH:MM]` and `/radar stats [days]` do the same, once the `/radar` slash command is added in
the Slack app settings (Slash Commands → Create New Command, then reinstall the app).

**The number:** time to first review on the live repo, from when the PR was opened (or marked ready) to the first
review by someone other than the author, bots excluded. "With Radar" counts PRs where the lead approved a Radar
handoff or review block before that first review; all other PRs count as "without Radar".

**Hours saved and cost** (last line of `/radar stats`): for each PR reviewed after a Radar action, the wait avoided is
`max(0, baseline − actual wait)`. The baseline is the live "without Radar" median once at least 3 such PRs exist,
otherwise `BASELINE_WAIT_HOURS` from the interview. Developer-hours = total wait avoided × `BLOCKED_COST_FACTOR`,
priced at `DEV_HOURLY_EUR`. Model cost uses the real token count of every LLM call (logged in `radar.db`) at
`LLM_USD_PER_M_INPUT` / `LLM_USD_PER_M_OUTPUT`. The footnote under the line shows every number used.

Automatic triggers (every `POLL_MINUTES`): a card before each of the lead's calendar events (`PRECALL_MINUTES`), and
for PRs waiting longer than `STALE_HOURS` while the lead is in a meeting. When the lead is free again and a card
is still unclicked, Radar replies once in the card's thread (PRs reviewed in the meantime are skipped). When a newer card
takes over a PR, that PR's row on the older card is greyed out right away (no buttons), so a PR is never live on
two cards. Every card, suggestion and decision is
logged in `radar.db` (SQLite).

# Review Radar

**When the engineering lead switches to client calls, the code keeps moving.**

At a small startup the engineering lead reviews every pull request *and* takes the client calls. On call-heavy days
PRs wait for hours, developers get blocked and lose context. Review Radar watches the lead's Google Calendar and the
team's GitHub repo, and at the moment of the hat switch (shortly before a call) proposes in Slack what should happen
to each PR waiting on the lead. **Nothing happens without the lead's click.**

- **Low risk** → hand the review to the best teammate: free in Google Calendar, knows the files, fewest open reviews.
- **High risk** (auth, payments, migrations, infra, CI, security, large diffs) → keep it with the lead and book the
  first free 30 minutes in his calendar after the calls.

Built for VibeHack Constanța 2026 (challenge *Multithreaded* by QA DNA). Decision: **automate**, using tools the team
already pays for (GitHub, Slack, Google Workspace); the only new cost is LLM usage.

## How it works

### 1. Triggers
| Trigger | When | What Radar does |
|---|---|---|
| **Pre-call** | a calendar event of the lead starts within `PRECALL_MINUTES` (default 10) | one card with every PR waiting on him (once per event) |
| **Stale PR** | the lead is in a meeting and a PR has waited more than `STALE_HOURS` (default 4) | a card with just those PRs |
| **Manual** | `/radar sweep [HH:MM]` in Slack or `radar sweep` in a terminal | a card now (the time is for demos: "pretend it is 16:50") |

Automatic checks run every `POLL_MINUTES` (default 5). Radar's own review blocks count as busy time but never as a
call, so they don't trigger pre-call cards. Automatic sweeps skip PRs that are already on an open card, snoozed, or
kept/blocked for the same commit.

### 2. Which PRs, and how risky
A PR is *waiting on the lead* when it is open, not a draft, and the lead is a requested reviewer (GitHub clears that
once he reviews). The wait starts when he was last requested. PRs that already have an upcoming Radar review block
are skipped: that decision is made.

**Risk** (`services/triage.py`): deterministic path rules run first (`core/rules.py`), then the LLM writes a 2-sentence
summary, a risk level, a reason and where to look first. The LLM can only *raise* risk, never lower it, and if it
fails or returns invalid JSON (retried once), Radar falls back to the rules and the PR title.

### 3. The suggestion
**Ranking** (`core/ranking.py`): every teammate except the lead and the PR's author, ordered by
1) free in Google Calendar for the next hour, 2) most commits to the PR's files in the last 90 days, 3) fewest open
review requests. Suggestions in the same sweep count toward that load, so work is spread.

**Decision:** low risk and the best candidate is free → hand off to them. Otherwise (high risk, or nobody free) →
keep it with the lead and offer the next free 30-minute slot within working hours (09:00–20:00 in the lead's
timezone, up to two days ahead). The longest-waiting PR gets the earliest slot.

### 4. The Slack card (DM to the lead)
Each PR shows its summary, risk, where to look first, the wait so far and Radar's suggestion, plus:
- **✅ Hand off to …** or **📅 Block 19:00–19:30 to review** (the day is named when it isn't today: "tomorrow 09:30–10:00")
- **🙋 Keep** and **⏰ Snooze 2h**
- **👥 Choose reviewer…**: every eligible engineer in ranked order (⭐ = Radar's pick), with the reasons listed one
  per line above the buttons. Works on high-risk PRs too, so the lead can still delegate.
- **✅ Approve all suggestions** when two or more PRs are still open on the card.

Only the lead's clicks count, double clicks are ignored, and the card updates in place to show who decided what and
when. When a newer card takes over a PR, its row on the older card is greyed out immediately (no buttons), so a PR is
never live on two cards. Link previews are turned off and PR authors are shown by their team name.

### 5. What an approval does (`services/actions.py`)
- **Handoff:** requests the review from the chosen engineer, removes the lead as reviewer, posts the summary as a PR
  comment, updates the merge gate and DMs the reviewer with the summary and a link to the diff.
- **Review block:** creates a "🔍 Review PR #N" event in the lead's calendar (tagged with the PR number) and comments on
  the PR. If the slot has passed by the time he clicks, the next free slot is used; if the PR already has an upcoming
  block, Radar reuses it instead of booking a second one.
- Before acting, Radar re-checks GitHub: a PR that was closed or no longer waits on the lead is marked "skipped".

### 6. Afterwards
- **Reminder:** once the lead is free again and a card is still unclicked, one reply in the card's thread (shown in
  the DM). PRs reviewed or closed in the meantime are marked skipped. Cards posted while he was free: after 30 min.
- **Review alerts** (`services/review_alerts.py`): when a teammate the lead handed a PR to submits a review, the lead
  gets a DM ("✅ Software Engineer 2 approved #7: the merge is unblocked"). Reviews that land during calls go into
  the digest instead. Checked every minute.
- **Post-call digest** (`services/digest.py`): when a busy stretch ends (and within 30 minutes of it), one DM
  summarising what each handoff and review block approved for that stretch led to (approved, changes requested,
  merged, still in progress…). Sent only if Radar acted during the stretch, at most once per stretch.
- **Merge gate** (`services/approval_gate.py`): a `radar/approval` commit status on every open PR that turns green only
  when the lead, or the teammate he handed the PR to, approved the latest commit. Nobody can approve their own PR; if
  the lead is the author, any teammate can. Make it a required status check in the repo's branch ruleset to enforce it.

## The number: `/radar stats [days]`

**Time to first review** on the live repo: from when a PR was opened (or last marked ready) to the first review by
someone other than the author, bots excluded. **With Radar** = PRs where the lead approved a Radar handoff or review
block *before* that first review; everything else is **without Radar**. Also shown: PRs still waiting, Radar's
decisions (suggested, approved, kept, snoozed, skipped because the PR was reviewed or closed first, open) and the lead's median time from card to decision (his own clicks only).

**Hours saved and cost** (last line, e.g. *"Saved ~4.5 developer-hours this week ≈ €90 vs ~$0.04 model cost"*):
for each PR reviewed after a Radar action, wait avoided = `max(0, baseline − actual wait)`. The baseline is the live
"without Radar" median once at least 3 such PRs exist, otherwise `BASELINE_WAIT_HOURS` from the interview.
Developer-hours = total wait avoided × `BLOCKED_COST_FACTOR`, priced at `DEV_HOURLY_EUR`. Model cost uses the real
token count of every LLM call (logged in `radar.db`) at `LLM_USD_PER_M_INPUT` / `LLM_USD_PER_M_OUTPUT`. The footnote
under the line prints every number used. `/radar stats 7` says "this week"; other values say "in the last N days".

**Buy vs automate** (the line after it, e.g. *"Buying instead: CodeRabbit Team ≈ $192/month (4 seats × $48) vs Radar
≈ $5.30/month (hosting $5 + model $0.30), and it doesn't reroute reviews around the lead's calls"*):
`BUY_TOOL_USD_PER_SEAT` × the people in `team.json` (per-seat tools bill everyone who opens PRs), against Radar's
monthly cost = `RADAR_HOSTING_USD_PER_MONTH` + the real model cost for the period projected to 30 days. Default: CodeRabbit Team, $48/user/month billed annually (coderabbit.ai/pricing, November 2026),
the tier that includes PR triage; its $24 Essentials tier only reviews code.

## The dashboard: Slack App Home

Open the Radar app in Slack and pick the **Home** tab. It is rebuilt every time the tab opens (data cached 60 s,
the number 10 min; **🔄 Refresh** forces a reload).

- **Every developer:** *👀 Waiting on you* (PRs where you're a requested reviewer, longest first, 🟢/🟡/🔴 by wait
  vs `STALE_HOURS`, with a wait bar and a *Review →* button), *📊 Your load* (open review requests vs the busiest
  teammate, calendar free/busy, your own open PRs and who they wait on) and *🤝 Handed to you* (Radar handoffs from the
  lead in the last 7 days: waiting, reviewed or closed).
- **The lead also sees:** *👥 Team load* (review requests, own PRs, free/busy and oldest wait per person),
  *🚧 Bottlenecks* (requests older than `STALE_HOURS`, one person holding half or more of all requests, busy people
  with reviews waiting), *💰 The number* for the last 7 days (hours saved, time to first review with vs without Radar
  as bars, decisions) and a **🛰️ Sweep now** button.

Code: `core/dashboard.py` (pure math), `services/dashboard.py` (snapshot + caches), `slack/home.py` (Block Kit view).

## Code layout

Each layer only imports from the layers listed after it, so the core logic is testable without any network.

```
radar/
  cli.py                 `radar` command (also `python -m radar`)
  slack/
    bot.py               Socket Mode app: buttons, /radar command, scheduler + merge-gate threads, shutdown
    messages.py          posts/updates cards; reminders, reviewer DMs, review alerts, post-call digest
    blocks.py            Block Kit rendering (pure)
  services/              use cases, orchestrating integrations + storage
    sweep.py             which PRs wait on the lead, what to suggest, the automatic triggers (tick)
    triage.py            risk = path rules first, the LLM can only raise it; logs token usage
    actions.py           every approved GitHub/Calendar write (handoff, review block, keep, snooze)
    reminders.py         one nudge per unclicked card once the lead is free
    review_alerts.py     DM the lead when a handed-off reviewer submits a review
    digest.py            "while you were in calls" summary after a busy stretch
    approval_gate.py     radar/approval commit status (merge gate)
    stats.py             the number, from GitHub history + the decision log
  storage/store.py       SQLite: cards, suggestions, decisions, triggers, LLM usage; versioned migrations
  integrations/          thin clients: github.py, google_calendar.py, google_auth.py, llm.py
  core/                  pure logic, no I/O: models, rules, ranking, schedule (busy/free math), metrics
  config.py              settings, team, file locations
  runtime.py             single-instance lock, heartbeat
scripts/                 google_login.py (one-time Google login), check_setup.py (tests every integration)
tests/                   one file per layer + helpers.py fixtures; external services are mocked
```

## Setup

### What you need
| Service | Setup |
|---|---|
| **GitHub App** | Installed on the live repo with *Pull requests: read & write*, *Commit statuses: read & write*, *Contents: read*, *Metadata: read*. Download its private key as `github-app.pem`. Every team member must be a collaborator. |
| **Slack app** | **Socket Mode on** (app-level token `xapp-…` with `connections:write`). Bot scopes: `chat:write`, `im:write`, `commands`, `users:read`, `users:read.email`. Interactivity on. Slash command `/radar` (usage hint `sweep [HH:MM] \| stats [days]`). **App Home:** turn on the *Home Tab* and subscribe to the bot event `app_home_opened` (Event Subscriptions). Reinstall the app after changing scopes or commands. Everyone in `team.json` must be in the workspace with the same email. |
| **Google** | A Google account for the bot, a Cloud project with the Calendar API and a *Desktop* OAuth client saved as `credentials.json`. The lead shares his calendar with the bot with *Make changes to events*; teammates share at least *See only free/busy*. Calendars the bot can't read count as busy. |
| **LLM** | Any OpenAI-compatible API. Tested with `openai/gpt-oss-120b` on Featherless; reasoning models need the generous `max_tokens` Radar uses. |

### Install
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -e .   # pinned dependencies + the `radar` command
cp .env.example .env                 # fill in values (see Configuration)
cp team.example.json team.json       # the lead + teammates: display name, GitHub login, Google/Slack email
.venv/bin/python -m scripts.google_login   # once: opens a browser, log in as the Google bot account
.venv/bin/python -m scripts.check_setup    # checks config, GitHub, Slack, Calendar, free/busy and the LLM
```
`team.json` names are what Slack messages show (e.g. "Engineering Lead", "Software Engineer 1"). `lead` is the
lead's GitHub login and must also be listed in `members`.

## Run

```bash
.venv/bin/radar run                        # Slack bot + automatic checks (only one per data directory, see below)
.venv/bin/radar sweep --at 16:50           # post a card now, as if it were 16:50 (demo)
.venv/bin/radar sweep --at 16:50 --dry-run # print suggestions, post nothing
.venv/bin/radar stats --days 7             # the number: time to first review, with vs without Radar
.venv/bin/radar remind --at 19:05          # send due reminders for unclicked cards now (demo)
.venv/bin/radar digest --at 18:35          # send the post-call digest now, if one is due (demo)
.venv/bin/radar gate                       # update the radar/approval merge-gate status on open PRs
.venv/bin/radar health                     # exit 0 if the running bot checked in recently
```

`python -m radar …` works the same without installing the package. Cards posted from the CLI are handled by the
running bot, so start `radar run` (or the container) first. Stop a local bot with `kill $(cat radar.lock)`; it shuts
down cleanly ("Review Radar shutting down" in the log).

In Slack: `/radar sweep [HH:MM]` and `/radar stats [days]`. Both answer at once with a short note only you see, then
post the result.

## Deploy with Docker

The image holds only code and pinned dependencies. Secrets are mounted read-only at runtime, and state
(`radar.db`, the refreshed Google token, the lock and heartbeat) lives in the `radar-data` volume.

```bash
# Needs .env, team.json, github-app.pem, credentials.json and token.json in the repo folder (see Setup).
docker compose up -d --build
docker compose logs -f radar
docker compose ps                          # shows "healthy" once the first check has run
docker compose run --rm radar python -m radar sweep --dry-run   # one-off commands use the same config
docker compose down                        # stop
```

- **Moving from a local bot:** the container starts with an empty database, so buttons on cards posted by a local
  bot won't work. To keep them, copy the database in before the first start:
  `docker compose create && docker compose cp radar.db radar:/data/radar.db && docker compose up -d`.
- **Code changes:** the container runs the image built from the current checkout; rebuild with
  `docker compose up -d --build` after pulling or switching branches.
- The entrypoint starts as root only to give `/data` to the `radar` user (uid 10001) and seed `token.json` on first
  start, then drops privileges; Radar and the health check never run as root.

## Operations

- **One bot only.** `radar run` holds a lock next to `radar.db` and refuses to start a second copy on the same data
  directory. It cannot see bots on other machines: if two people run Radar against the same Slack app, button
  clicks are split between them at random. Agree on one host.
- **Health:** the scheduler writes a heartbeat after every check; `radar health` (the Docker health check) fails if
  none arrived within two check intervals plus 2 minutes.
- **Shutdown:** SIGTERM (`kill`, `docker compose stop`) closes the Slack connection and stops the loops cleanly.
- **Database:** SQLite in WAL mode; the schema is versioned (`PRAGMA user_version`) and migrated automatically on
  start. If the file isn't writable, Radar exits with a message saying which file and user.
- **Network:** Google Calendar calls use one client per thread and a 30 s timeout, so a hung request can't freeze the
  scheduler. LLM calls time out after 60 s and run at most 2 at a time.
- **Secrets on a shared host:** keep `github-app.pem`, `token.json`, `credentials.json` and `.env` readable only by
  you (`chmod 600`). The container user must still be able to read the mounted files, so on shared hosts prefer
  Docker secrets or a host directory owned by uid 10001.
- **Dependencies:** direct ones with ranges in `pyproject.toml`; exact tested versions in `requirements.txt`. To
  update: change `pyproject.toml`, install in a fresh virtualenv, run the tests, then
  `pip freeze --exclude-editable > requirements.txt`.

## Configuration

All settings live in `.env` (see `.env.example`); real environment variables win over it.

| Variable | Default | Meaning |
|---|---|---|
| `GITHUB_APP_ID`, `GITHUB_APP_KEY_PATH` | –, `github-app.pem` | GitHub App ID and private key |
| `LIVE_REPO` | – | `owner/repo` Radar watches and acts on |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | – | OpenAI-compatible endpoint |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | – | `xoxb-…` bot token and `xapp-…` Socket Mode token |
| `GOOGLE_BOT_EMAIL` | – | the bot's Google account (`check_setup` verifies the login matches) |
| `PRECALL_MINUTES` | 10 | how long before a call the pre-call card is posted |
| `STALE_HOURS` | 4 | wait after which a PR gets its own card while the lead is in a meeting |
| `POLL_MINUTES` | 5 | how often the automatic checks run |
| `LOG_LEVEL` | INFO | Python log level |
| `BASELINE_WAIT_HOURS` | 8 | typical wait without Radar (from the interview), until the live repo has 3+ PRs reviewed without Radar |
| `BLOCKED_COST_FACTOR` | 0.25 | developer-hours lost per hour a PR waits |
| `DEV_HOURLY_EUR` | 20 | developer cost per hour |
| `LLM_USD_PER_M_INPUT`, `LLM_USD_PER_M_OUTPUT` | 0.15, 0.60 | model price per 1M tokens: typical pay-as-you-go rate for gpt-oss-120b (Groq, Together, Amazon Bedrock; openrouter.ai, Nov 2026) |
| `BUY_TOOL_NAME`, `BUY_TOOL_USD_PER_SEAT` | CodeRabbit Team, 48 | the per-seat tool in the "Buying instead" line |
| `RADAR_HOSTING_USD_PER_MONTH` | 5 | Radar's hosting cost (small VPS), added to model usage in that line |
| `RADAR_HOME` | the repo folder | where `.env`, `team.json` and the other files are looked up |
| `RADAR_TEAM_FILE`, `RADAR_DB_PATH`, `GOOGLE_TOKEN_PATH`, `GOOGLE_CREDENTIALS_PATH` | `team.json`, `radar.db`, `token.json`, `credentials.json` | file locations; relative paths resolve against `RADAR_HOME`; lock and heartbeat live next to the database |
| `GITHUB_TOKEN` (or `GITHUB_TOKEN_1`), `BEFORE_REPO` | – | reserved for the "before" metric on a historical repo; **read but not used yet** |

Fixed in code: working hours 09:00–20:00, review blocks of 30 minutes, snooze of 2 hours, file history window of
90 days, "free" means free for the next 60 minutes.

## Tests

```bash
.venv/bin/python -m unittest discover tests
```
`test_core.py` (rules, calendar math, ranking, metrics), `test_services.py` (triage, reminders, merge gate, digest,
review alerts, actions; GitHub/Calendar/LLM mocked), `test_slack.py` (rendering, posting, the `/radar` command),
`test_storage_runtime.py` (migrations, lock, heartbeat, file locations).

## Known limitations

- One repo (`LIVE_REPO`) and one lead per deployment.
- The "before" number from a historical repo (`BEFORE_REPO`) is not implemented yet; `/radar stats` uses the live repo
  and falls back to `BASELINE_WAIT_HOURS`.
- The single-instance lock only covers one machine (see Operations).
- Radar never reviews code itself: the LLM summary and "look first" hint are context for the human reviewer.

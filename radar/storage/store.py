"""SQLite log of every card, suggestion and decision. Statuses: pending, working, handed_off, blocked, kept, snoozed, stale, failed, superseded."""
import datetime as dt
import sqlite3
from contextlib import contextmanager

from radar import config
from radar.core.models import Suggestion

DB_PATH = config.DB_PATH
SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    trigger TEXT NOT NULL,
    header TEXT NOT NULL,
    channel TEXT,
    ts TEXT
);
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY,
    card_id INTEGER NOT NULL REFERENCES cards(id),
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    action TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    suggested_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    snoozed_until TEXT,
    result TEXT,
    handed_to TEXT
);
CREATE TABLE IF NOT EXISTS fired_triggers (
    key TEXT PRIMARY KEY,
    fired_at TEXT NOT NULL
);
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _add_handed_to(c: sqlite3.Connection) -> None:
    columns = {row["name"] for row in c.execute("PRAGMA table_info(suggestions)")}
    if "handed_to" not in columns:
        c.execute("ALTER TABLE suggestions ADD COLUMN handed_to TEXT")


# Schema version N is reached by running MIGRATIONS[:N] in order; the version is kept in PRAGMA user_version.
# Every step must be safe on databases created before versioning existed. Append new steps, never edit old ones.
MIGRATIONS = [
    lambda c: c.executescript(SCHEMA),
    _add_handed_to,
]


def schema_version() -> int:
    with connect() as c:
        return c.execute("PRAGMA user_version").fetchone()[0]


def init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        # WAL lets the scheduler, Slack handlers and CLI commands read while another one writes.
        c.execute("PRAGMA journal_mode=WAL")
        version = c.execute("PRAGMA user_version").fetchone()[0]
        for step in MIGRATIONS[version:]:
            step(c)
        c.execute(f"PRAGMA user_version = {len(MIGRATIONS)}")


def create_card(trigger: str, header: str, suggestions: list[Suggestion]) -> int:
    with connect() as c:
        card_id = c.execute("INSERT INTO cards (created_at, trigger, header) VALUES (?, ?, ?)",
                            (_now(), trigger, header)).lastrowid
        for sg in suggestions:
            c.execute("UPDATE suggestions SET status = 'superseded' WHERE pr_number = ? AND status = 'pending'",
                      (sg.pr.number,))
            c.execute(
                "INSERT INTO suggestions (card_id, pr_number, head_sha, action, payload, suggested_at) VALUES (?, ?, ?, ?, ?, ?)",
                (card_id, sg.pr.number, sg.pr.head_sha, sg.action, sg.to_json(), _now()),
            )
    return card_id


def set_card_message(card_id: int, channel: str, ts: str) -> None:
    with connect() as c:
        c.execute("UPDATE cards SET channel = ?, ts = ? WHERE id = ?", (channel, ts, card_id))


def card(card_id: int) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()


def card_suggestions(card_id: int) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT * FROM suggestions WHERE card_id = ? ORDER BY id", (card_id,)).fetchall()


def suggestion(sid: int) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM suggestions WHERE id = ?", (sid,)).fetchone()


def claim(sid: int, user: str) -> bool:
    """Atomically move a pending suggestion to 'working'. False if someone already decided it (double clicks)."""
    with connect() as c:
        cur = c.execute(
            "UPDATE suggestions SET status = 'working', decided_by = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
            (user, _now(), sid),
        )
        return cur.rowcount == 1


def finish(sid: int, status: str, result: str = "", snoozed_until: dt.datetime | None = None,
           handed_to: str | None = None) -> None:
    with connect() as c:
        c.execute(
            "UPDATE suggestions SET status = ?, result = ?, snoozed_until = ?, decided_at = ?, handed_to = ? WHERE id = ?",
            (status, result, snoozed_until.isoformat() if snoozed_until else None, _now(), handed_to, sid),
        )


def recently_handled(pr_number: int, head_sha: str, now: dt.datetime) -> bool:
    """True if automatic sweeps should skip this PR: it has an open card, the lead already decided this
    version of it, or it is snoozed."""
    with connect() as c:
        rows = c.execute("SELECT status, head_sha, snoozed_until FROM suggestions WHERE pr_number = ?",
                         (pr_number,)).fetchall()
    for r in rows:
        if r["status"] in ("pending", "working"):
            return True
        if r["status"] == "snoozed" and r["snoozed_until"] and dt.datetime.fromisoformat(r["snoozed_until"]) > now:
            return True
        if r["status"] in ("kept", "blocked") and r["head_sha"] == head_sha:
            return True
    return False


def fire_once(key: str) -> bool:
    """Records a trigger. False if it already fired (e.g. the same calendar event)."""
    with connect() as c:
        cur = c.execute("INSERT OR IGNORE INTO fired_triggers (key, fired_at) VALUES (?, ?)", (key, _now()))
        return cur.rowcount == 1


def all_suggestions() -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT * FROM suggestions ORDER BY id").fetchall()


def handed_off(pr_number: int) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT * FROM suggestions WHERE pr_number = ? AND status = 'handed_off'",
                         (pr_number,)).fetchall()


def pending_by_card() -> dict[int, list[sqlite3.Row]]:
    with connect() as c:
        rows = c.execute("SELECT * FROM suggestions WHERE status = 'pending' ORDER BY card_id, id").fetchall()
    result: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        result.setdefault(row["card_id"], []).append(row)
    return result

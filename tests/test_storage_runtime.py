"""radar.storage, radar.runtime and radar.config: database migrations, process safety, file locations."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from radar import config, runtime
from radar.storage import store


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_second_instance_is_refused_and_lock_is_released(self):
        lock = self.dir / "radar.lock"
        with runtime.single_instance(lock):
            with self.assertRaises(runtime.AlreadyRunning) as e:
                with runtime.single_instance(lock):
                    pass
            self.assertIn(str(os.getpid()), str(e.exception))
        with runtime.single_instance(lock):  # free again after the first holder exits
            pass

    def test_heartbeat(self):
        beat = self.dir / "radar.heartbeat"
        self.assertIsNone(runtime.heartbeat_age(beat))
        runtime.beat(beat)
        self.assertLess(runtime.heartbeat_age(beat), 5)
        self.assertEqual(runtime.healthy_within(5), 720)


class MigrationTest(unittest.TestCase):
    def test_upgrades_a_pre_versioning_database_and_is_idempotent(self):
        db = Path(tempfile.mkdtemp()) / "old.db"
        old = sqlite3.connect(db)  # schema as it was before versioning: no handed_to, user_version 0
        old.executescript(store.SCHEMA.replace(",\n    handed_to TEXT", ""))
        old.execute("INSERT INTO cards (created_at, trigger, header) VALUES ('x', 'manual', 'h')")
        old.commit()
        old.close()
        with mock.patch.object(store, "DB_PATH", db):
            store.init()
            store.init()
            self.assertEqual(store.schema_version(), len(store.MIGRATIONS))
            with store.connect() as c:
                columns = {r["name"] for r in c.execute("PRAGMA table_info(suggestions)")}
                self.assertIn("handed_to", columns)
                self.assertEqual(c.execute("SELECT count(*) FROM cards").fetchone()[0], 1)
                self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "wal")


class PathSettingTest(unittest.TestCase):
    def test_relative_paths_resolve_against_root_and_absolute_paths_win(self):
        with mock.patch.dict(os.environ, {"X_PATH": "data/x.db"}):
            self.assertEqual(config.path_setting("X_PATH", "d"), config.ROOT / "data/x.db")
        with mock.patch.dict(os.environ, {"X_PATH": "/data/x.db"}):
            self.assertEqual(config.path_setting("X_PATH", "d"), Path("/data/x.db"))
        self.assertEqual(config.path_setting("UNSET_PATH_SETTING", "team.json"), config.ROOT / "team.json")


if __name__ == "__main__":
    unittest.main()

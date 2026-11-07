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


class ReadOnlyDatabaseTest(unittest.TestCase):
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can write read-only files")
    def test_readonly_database_gives_a_clear_error(self):
        db = Path(tempfile.mkdtemp()) / "ro.db"
        sqlite3.connect(db).close()
        db.chmod(0o444)
        self.addCleanup(db.chmod, 0o644)
        with mock.patch.object(store, "DB_PATH", db):
            with self.assertRaises(store.NotWritable) as e:
                store.init()
        self.assertIn("read-only", str(e.exception))


class LlmUsageStoreTest(unittest.TestCase):
    def test_sums_usage_since(self):
        import datetime as dt
        from radar.core.models import TokenUsage
        with mock.patch.object(store, "DB_PATH", Path(tempfile.mkdtemp()) / "t.db"):
            store.init()
            with mock.patch.object(store, "_now", return_value="2026-11-01T10:00:00+00:00"):
                store.record_llm_usage("m", 1, TokenUsage(1000, 100))
            store.record_llm_usage("m", 2, TokenUsage(500, 50))
            since = dt.datetime(2026, 11, 5, tzinfo=dt.timezone.utc)
            self.assertEqual(store.llm_usage_since(since), TokenUsage(500, 50))


class PathSettingTest(unittest.TestCase):
    def test_relative_paths_resolve_against_root_and_absolute_paths_win(self):
        with mock.patch.dict(os.environ, {"X_PATH": "data/x.db"}):
            self.assertEqual(config.path_setting("X_PATH", "d"), config.ROOT / "data/x.db")
        with mock.patch.dict(os.environ, {"X_PATH": "/data/x.db"}):
            self.assertEqual(config.path_setting("X_PATH", "d"), Path("/data/x.db"))
        self.assertEqual(config.path_setting("UNSET_PATH_SETTING", "team.json"), config.ROOT / "team.json")


if __name__ == "__main__":
    unittest.main()

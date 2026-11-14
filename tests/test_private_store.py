"""B1 : fixtures privées temporaires, identités synthétiques, aucun transport."""
from contextlib import ExitStack, closing
import copy
import json
import os
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools.windows_watcher import private_store as store


NOW = 1800000000
CONFIG = {"mode": "simulation", "send_enabled": False, "notifications_channel_id": None,
          "owner_user_id": 12345, "arbitration_private_chat_id": 12345}
BOT = 54321


def update(identity=100, message=1, date=NOW, text="/status"):
    return {"update_id": identity, "message": {"message_id": message, "date": date, "text": text,
            "from": {"id": 12345, "is_bot": False}, "chat": {"id": 12345, "type": "private"}}}


CHILD = r'''
import os, sys
from tools.windows_watcher.private_store import SimulationStore, StoreError
root, stage = sys.argv[1:]
config = dict(mode="simulation", send_enabled=False, notifications_channel_id=None,
              owner_user_id=12345, arbitration_private_chat_id=12345)
message = {"update_id": 100, "message": {"message_id": 1, "date": 1800000000,
           "text": "/status", "from": {"id": 12345, "is_bot": False},
           "chat": {"id": 12345, "type": "private"}}}
def crash(point):
    if point == stage:
        os._exit(77)
try:
    with SimulationStore(root, config, 54321, 1800000000, fault=crash) as session:
        if stage == "lock":
            raise RuntimeError("second writer admitted")
        session.receive_batch([message], 0, 1800000000)
except StoreError as error:
    print(str(error))
    sys.exit(23)
'''


class PrivateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="phase823-b1-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "fixture"

    def session(self, **options):
        return store.SimulationStore(self.root, CONFIG, BOT, options.pop("now", NOW), **options)

    def initialize(self, **options):
        with self.session(create=True, **options) as session:
            return session.status()

    def raw(self, sql, parameters=()):
        with closing(sqlite3.connect(self.root / "fixtures.sqlite3")) as connection:
            result = connection.execute(sql, parameters).fetchall()
            connection.commit()
            return result

    def child(self, stage):
        return subprocess.run([sys.executable, "-c", CHILD, str(self.root), stage],
                              capture_output=True, text=True, timeout=15,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_effective_sqlite_settings_schema_and_isolation(self):
        with self.session(create=True) as session:
            self.assertEqual(session.settings(), {"journal_mode": "delete", "synchronous": 3,
                             "foreign_keys": 1, "temp_store": 2, "trusted_schema": 0, "busy_timeout": 100})
            self.assertEqual(session.status()["next_offset"], 0)
            self.assertTrue(session.status()["simulation_only"])
        self.assertEqual(self.raw("PRAGMA user_version"), [(1,)])
        self.assertEqual(self.raw("PRAGMA integrity_check"), [("ok",)])
        self.assertFalse((self.root / "fixtures.sqlite3-wal").exists())

    def test_atomic_empty_repeated_noncontiguous_batches_and_restart(self):
        with self.session(create=True) as session:
            self.assertEqual(session.receive_batch([], 0, NOW)["next_offset"], 0)
            batch = [update(100, 1), update(104, 2)]
            first = session.receive_batch(batch, 0, NOW)
            self.assertEqual((first["next_offset"], first["new_messages"]), (105, 2))
        with self.session() as session:
            repeated = session.receive_batch(batch, 105, NOW)
            self.assertEqual((repeated["new_messages"], repeated["new_receipts"]), (0, 0))
            duplicate_message = session.receive_batch([update(110, 1)], 105, NOW)
            self.assertEqual(duplicate_message["new_messages"], 0)
            self.assertEqual((duplicate_message["receipts"], duplicate_message["messages"]), (3, 2))

    def test_rejected_messages_have_minimal_receipts_without_identity_or_text(self):
        candidates = [update(100), {"update_id": 101, "channel_post": {"secret": "PRIVATE_SECRET"}},
                      {"update_id": 102, "message": {}}, update(103)]
        candidates[0]["message"]["from"]["id"] = 99999
        candidates[0]["message"]["text"] = "PRIVATE_SECRET"
        candidates[3]["message"]["forward_origin"] = None
        with self.session(create=True) as session:
            result = session.receive_batch(candidates, 0, NOW)
            self.assertEqual((result["new_messages"], result["receipts"], result["next_offset"]), (0, 4, 104))
        rows = self.raw("SELECT digest,message_id,disposition FROM receipts")
        self.assertEqual(rows, [(None, None, "rejected")] * 4)
        self.assertNotIn(b"PRIVATE_SECRET", (self.root / "fixtures.sqlite3").read_bytes())

    def test_malformed_batch_and_order_never_advance_cursor(self):
        bad = [None, {}, "secret", [None], [{"update_id": True}], [{"update_id": -1}],
               [{"update_id": "100"}], [update(101), update(100)], [update(), update()],
               [update(i, i) for i in range(21)]]
        for index, batch in enumerate(bad):
            with self.subTest(batch=index):
                self.root = Path(self.temporary.name) / str(index)
                self.initialize()
                with self.session() as session:
                    with self.assertRaises(store.StoreError):
                        session.receive_batch(batch, 0, NOW)
                    with self.assertRaises(store.StoreError):
                        session.status()  # Session fautive non réutilisable.
                with self.session() as session:
                    result = session.status()
                    self.assertEqual((result["stored_offset"], result["receipts"]), (0, 0))
                    self.assertIsNone(result["next_offset"])

    def test_collisions_regression_and_stale_expected_offset_suspend(self):
        bad = [(update(100, 1, text="/help"), 101), (update(101, 1, text="/help"), 101),
               (update(50, 2), 101), (update(101, 2), 0)]
        for index, (candidate, expected) in enumerate(bad):
            self.root = Path(self.temporary.name) / str(index)
            with self.session(create=True) as session:
                session.receive_batch([update()], 0, NOW)
                with self.assertRaises(store.StoreError):
                    session.receive_batch([candidate], expected, NOW)
            with self.session() as session:
                self.assertEqual(session.status()["stored_offset"], 101)
                self.assertEqual(session.status()["state"], "recovery_required")
                self.assertEqual(session.status()["messages"], 1)

    def test_crash_process_before_during_and_after_commit(self):
        for stage in ("before_begin", "after_receipt", "before_commit", "after_commit"):
            with self.subTest(stage=stage):
                self.root = Path(self.temporary.name) / stage
                self.initialize()
                child = self.child(stage)
                self.assertEqual(child.returncode, 77, child.stderr)
                with self.session() as session:
                    result = session.status()
                    committed = stage == "after_commit"
                    self.assertEqual(result["stored_offset"], 101 if committed else 0)
                    self.assertEqual(result["receipts"], int(committed))
                    replay = session.receive_batch([update()], result["stored_offset"], NOW)
                    self.assertEqual(replay["new_messages"], 0 if committed else 1)

    def test_fault_exception_rolls_back_entire_batch(self):
        def fail(stage):
            if stage == "after_receipt":
                raise OSError("sensitive filesystem failure")
        with self.session(create=True, fault=fail) as session:
            with self.assertRaisesRegex(store.StoreError, "^STORAGE_FAILED$"):
                session.receive_batch([update(), update(101, 2)], 0, NOW)
        with self.session() as session:
            self.assertEqual((session.status()["receipts"], session.status()["stored_offset"]), (0, 0))

    def test_two_processes_cannot_hold_the_same_lock(self):
        with self.session(create=True):
            child = self.child("lock")
            self.assertEqual(child.returncode, 23, child.stderr)
            self.assertEqual(child.stdout.strip(), "LOCKED")
        with self.session() as session:
            self.assertEqual(session.status()["next_offset"], 0)

    def test_exact_expiration_retains_receipts_messages_and_cursor(self):
        with self.session(create=True, now=NOW-1000) as session:
            result = session.receive_batch([update(100, 1, NOW-120), update(101, 2, NOW-121)], 0, NOW)
            self.assertEqual(result["pending"], 1)
            session.resume(NOW+0.001)
            self.assertEqual(session.status()["pending"], 0)
            self.assertEqual((session.status()["receipts"], session.status()["messages"],
                              session.status()["stored_offset"]), (2, 2, 102))
        with self.session(now=NOW+1) as session:
            replay = session.receive_batch([update(100, 1, NOW-120)], 102, NOW+1)
            self.assertEqual(replay["new_messages"], 0)

    def test_historical_and_future_commands_and_clock_rollback(self):
        with self.session(create=True) as session:
            session.receive_batch([update(date=NOW-1)], 0, NOW)
            self.assertEqual(session.status()["pending"], 0)
            self.assertEqual(self.raw("SELECT status FROM messages"), [("historical",)])
            with self.assertRaisesRegex(store.StoreError, "CLOCK_FUTURE"):
                session.receive_batch([update(101, 2, NOW+31)], 101, NOW)
        with self.session(now=NOW-1) as session:
            self.assertEqual(session.status()["state"], "recovery_required")

    def test_resume_after_three_hours_day_days_and_week(self):
        for delay, expected in ((3*3600, "normal"), (86400, "gap_possible"),
                                (3*86400, "gap_possible"), (7*86400, "recovery_required")):
            self.root = Path(self.temporary.name) / str(delay)
            self.initialize()
            with self.session(now=NOW+delay) as session:
                self.assertEqual(session.status()["state"], expected)
                if expected != "recovery_required":
                    session.receive_batch([], 0, NOW+delay)
                    self.assertEqual(session.status()["state"], expected)
                else:
                    self.assertIsNone(session.status()["next_offset"])

    def test_manual_epoch_recovery_is_atomic_and_preserves_message_uniqueness(self):
        with self.session(create=True) as session:
            session.receive_batch([update(10000, 1)], 0, NOW)
        with self.session(now=NOW+7*86400) as session:
            with self.assertRaisesRegex(store.StoreError, "APPROVAL_REQUIRED"):
                session.recover_epoch([update(2, 1)], NOW+7*86400, approval="no")
            result = session.recover_epoch([update(2, 1), update(3, 2)], NOW+7*86400,
                                           approval="reviewed-simulation-recovery")
            self.assertEqual((result["epoch"], result["next_offset"], result["messages"]), (1, 4, 2))
            self.assertEqual(result["pending"], 0)
            self.assertEqual(result["state"], "gap_possible")
        with self.session(now=NOW+7*86400) as session:
            self.assertEqual(session.status()["messages"], 2)
            self.assertEqual(self.raw("SELECT offset FROM epochs ORDER BY id"), [(10001,), (4,)])

    def test_recovery_crash_rolls_back_epoch_and_receipts(self):
        self.initialize()
        def fail(stage):
            if stage == "before_commit":
                raise OSError("fault")
        with self.session(now=NOW+7*86400, fault=fail) as session:
            with self.assertRaises(store.StoreError):
                session.recover_epoch([update()], NOW+7*86400, approval="reviewed-simulation-recovery")
        with self.session(now=NOW+7*86400) as session:
            self.assertEqual((session.status()["epoch"], session.status()["receipts"]), (0, 0))

    def test_corrupt_incompatible_missing_and_inconsistent_stores_are_not_reset(self):
        cases = ("corrupt", "schema", "missing", "cursor", "payload", "binding", "reason", "receipt")
        for case in cases:
            self.root = Path(self.temporary.name) / case
            with self.session(create=True) as session:
                session.receive_batch([update()], 0, NOW)
            path = self.root / "fixtures.sqlite3"
            if case == "corrupt":
                path.write_bytes(b"not a database")
            elif case == "missing":
                path.unlink()
            elif case == "schema":
                self.raw("PRAGMA user_version=99")
            elif case == "cursor":
                self.raw("UPDATE epochs SET offset=999")
            elif case == "payload":
                self.raw("UPDATE messages SET digest='bad'")
            elif case == "receipt":
                self.raw("UPDATE receipts SET digest='bad'")
            elif case == "reason":
                meta = json.loads(self.raw("SELECT data FROM meta")[0][0])
                meta["reason"] = "PRIVATE_SECRET"
                self.raw("UPDATE meta SET data=?", (json.dumps(meta),))
            else:
                (self.root / "fixture-identity.json").write_text("{}", encoding="utf-8")
            before = path.read_bytes() if path.exists() else None
            with self.assertRaises(store.StoreError):
                with self.session():
                    pass
            self.assertEqual(path.read_bytes() if path.exists() else None, before)
            with self.assertRaises(store.StoreError):
                with self.session(create=True):
                    pass

    def test_real_sqlite_full_rolls_back_receipts_and_offset(self):
        with self.session(create=True) as session:
            pages = session.connection.execute("PRAGMA page_count").fetchone()[0]
            session.connection.execute(f"PRAGMA max_page_count={pages}")
            batch = [update(100+i, i+1, text="x"*256) for i in range(20)]
            with self.assertRaisesRegex(store.StoreError, "STORAGE_FAILED") as caught:
                session.receive_batch(batch, 0, NOW)
            self.assertEqual(caught.exception.__context__.sqlite_errorcode, sqlite3.SQLITE_FULL)
        self.assertEqual(self.raw("SELECT offset FROM epochs"), [(0,)])
        self.assertEqual(self.raw("SELECT count(*) FROM receipts"), [(0,)])

    def test_quotas_are_checked_before_commit_without_eviction(self):
        for option in ({"receipt_limit": 1}, {"pending_limit": 1}):
            self.root = Path(self.temporary.name) / next(iter(option))
            with self.session(create=True, **option) as session:
                with self.assertRaisesRegex(store.StoreError, "QUOTA_EXCEEDED"):
                    session.receive_batch([update(), update(101, 2)], 0, NOW)
            self.assertEqual(self.raw("SELECT count(*) FROM receipts"), [(0,)])
            self.assertEqual(self.raw("SELECT offset FROM epochs"), [(0,)])
        self.root = Path(self.temporary.name) / "bytes"
        with self.assertRaises(store.StoreError):
            with self.session(create=True, database_bytes=1):
                pass

    def test_acl_annexes_and_unexpected_files_fail_closed(self):
        with self.session(create=True) as session:
            paths = []
            with patch.object(store.credential_acl, "verify", side_effect=lambda path: paths.append(path)):
                with session._transaction():
                    session.connection.execute("UPDATE epochs SET reason='fixture'")
                    self.assertTrue(session.windows_acl_readiness()["acl_ready"])
                    self.assertIn(self.root / "fixtures.sqlite3-journal", paths)
            with patch.object(store.credential_acl, "verify", side_effect=ValueError("sensitive")):
                self.assertFalse(session.windows_acl_readiness()["acl_ready"])
        (self.root / "fixtures.sqlite3-wal").write_bytes(b"unexpected")
        with self.assertRaises(store.StoreError):
            with self.session():
                pass

    def test_reparse_and_hardlink_detection_without_changing_permissions(self):
        with patch.object(Path, "lstat", return_value=SimpleNamespace(
                st_mode=stat.S_IFDIR, st_file_attributes=0x400)):
            with self.assertRaisesRegex(store.StoreError, "PATH_UNSAFE"):
                store._safe_path(self.temporary.name)
        self.initialize()
        os.link(self.root / "fixture-identity.json", Path(self.temporary.name) / "other-link")
        with self.assertRaisesRegex(store.StoreError, "FILES_UNSAFE"):
            with self.session():
                pass

    def test_identity_mode_and_reserved_production_paths_are_rejected(self):
        for config in (None, {}, {**CONFIG, "send_enabled": True},
                       {**CONFIG, "owner_user_id": None}, {**CONFIG, "token": "fixture-secret"}):
            with self.assertRaisesRegex(store.StoreError, "IDENTITY_INVALID"):
                store.SimulationStore(self.root, config, BOT, NOW)
        for bad_time in (True, None, "now", -1, float("nan"), float("inf"), 10**1000):
            with self.assertRaisesRegex(store.StoreError, "CLOCK_INVALID"):
                store.SimulationStore(self.root, CONFIG, BOT, bad_time)
        with patch.object(Path, "stat", side_effect=AssertionError("no production stat")):
            with self.assertRaisesRegex(store.StoreError, "PATH_UNSAFE"):
                store.SimulationStore(Path(self.temporary.name) / "NewsTelegramWatcher", CONFIG, BOT, NOW)

    @unittest.skipUnless(os.name == "nt", "Windows ACL observation")
    def test_windows_fixture_acl_can_be_inspected_without_changing_permissions(self):
        with self.session(create=True) as session:
            result = session.windows_acl_readiness()
            self.assertIs(type(result["acl_ready"]), bool)
            print("B1 Windows SQLite", sqlite3.sqlite_version, "PRAGMA", session.settings(),
                  "fixture ACL ready:", result["acl_ready"])

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_posix_permissions_are_restrictive_and_unsafe_modes_refused(self):
        self.initialize()
        for path in self.root.iterdir():
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        (self.root / "fixtures.sqlite3").chmod(0o644)
        with self.assertRaises(store.StoreError):
            with self.session():
                pass

    def test_guarded_fixture_io_no_network_environment_token_or_production(self):
        original_open = __import__("builtins").open
        original_io_open = __import__("io").open
        def guarded(opener):
            def open_fixture(path, *args, **kwargs):
                if not Path(path).absolute().is_relative_to(Path(self.temporary.name)):
                    raise AssertionError("non-fixture file")
                return opener(path, *args, **kwargs)
            return open_fixture
        message = update()
        before = copy.deepcopy((message, CONFIG))
        with ExitStack() as stack:
            for target in ("socket.socket", "urllib.request.urlopen", "requests.sessions.Session.request",
                           "subprocess.Popen", "os.getenv", "os._Environ.__getitem__", "time.time"):
                stack.enter_context(patch(target, side_effect=AssertionError("forbidden")))
            stack.enter_context(patch("builtins.open", guarded(original_open)))
            stack.enter_context(patch("io.open", guarded(original_io_open)))
            with self.session(create=True) as session:
                session.receive_batch([message], 0, NOW)
        self.assertEqual((message, CONFIG), before)

    def test_randomized_replay_model_survives_session_restarts(self):
        self.initialize()
        rng = random.Random(8231)
        next_id = 100
        known = []
        for index in range(30):
            with self.session() as session:
                old = session.status()
                if known and rng.choice((True, False)):
                    batch = [rng.choice(known)]
                    expected_new = 0
                else:
                    candidate = update(next_id, index+1)
                    known.append(candidate)
                    next_id += rng.randint(1, 10)
                    batch = [candidate]
                    expected_new = 1
                result = session.receive_batch(batch, old["next_offset"], NOW)
                self.assertEqual(result["new_messages"], expected_new)
                self.assertEqual(result["receipts"], len(known))


if __name__ == "__main__":
    unittest.main()

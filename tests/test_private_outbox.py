"""B2 : outbox et serveur exclusivement simulés, sans credential ni réseau."""
from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import datetime, timezone
import copy
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.windows_watcher import private_outbox as outbox, private_store as base


NOW = 1800000000
BOT = 54321
GENERATION = "11111111-1111-4111-8111-111111111111"
OTHER_GENERATION = "22222222-2222-4222-8222-222222222222"
CONFIG = dict(mode="simulation", send_enabled=False, notifications_channel_id=None,
              owner_user_id=12345, arbitration_private_chat_id=12345)
PERMISSION = outbox.SimulationPermission(BOT, 12345, 12345, GENERATION, True)


def message(number=1, text="/status", date=NOW):
    return {"update_id": 100+number, "message": {"message_id": number, "text": text, "date": date,
            "from": {"id": 12345, "is_bot": False}, "chat": {"id": 12345, "type": "private"}}}


CHILD = r'''
import os,sys
from tools.windows_watcher.private_outbox import DeliveryStore, ScriptedTransport, SimulationPermission
root, phase, stage = sys.argv[1:]
config = dict(mode="simulation",send_enabled=False,notifications_channel_id=None,
              owner_user_id=12345,arbitration_private_chat_id=12345)
generation="11111111-1111-4111-8111-111111111111"
def crash(point):
    if point == stage:
        os._exit(77)
with DeliveryStore(root,config,54321,1800000000,generation=generation,migrate=True,fault=crash) as session:
    if phase == "prepare":
        session.prepare(1,None,1800000000)
    elif phase == "deliver":
        session.deliver(1,ScriptedTransport(),1800000000,
                        permission=SimulationPermission(54321,12345,12345,generation,True))
'''


class PrivateOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="phase823-b2-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "fixture"
        self.snapshot = {"initialized": True, "last_poll_at": datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
                         "inbox": [], "pr_snapshots": {"10": {"head_sha": "a"*40, "closed": False}},
                         "ci_snapshots": {"a"*40: {"result": "pending", "checked_at": NOW}},
                         "coordination_registry": {}}

    def session(self, **kwargs):
        return outbox.DeliveryStore(self.root, CONFIG, BOT, kwargs.pop("now", NOW),
                                   generation=kwargs.pop("generation", GENERATION), **kwargs)

    def initialize(self, prepared=False):
        with self.session(create=True) as session:
            session.receive_batch([message()], 0, NOW)
            if prepared:
                session.prepare(1, self.snapshot, NOW)

    def raw(self, sql, values=()):
        with closing(sqlite3.connect(self.root / "fixtures.sqlite3")) as connection:
            result = connection.execute(sql, values).fetchall()
            connection.commit()
            return result

    def child(self, phase, stage):
        return subprocess.run([sys.executable, "-c", CHILD, str(self.root), phase, stage],
                              capture_output=True, text=True, timeout=15,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_six_commands_filters_and_private_generation_frozen(self):
        commands = ["/start", "/help", "/status", "/pr", "/ci", "/rapports", "/pr 10", "/ci 10",
                    "/ci " + "a"*40, "/rapports " + GENERATION]
        with self.session(create=True) as session:
            session.receive_batch([message(i+1, command) for i, command in enumerate(commands)], 0, NOW)
            for i, command in enumerate(commands):
                result = session.prepare(i+1, self.snapshot, NOW)
                self.assertEqual(result["code"], "ok", command)
                frozen = session.inspect_outbox(i+1)
                self.assertEqual(frozen["destination"], 12345)
                self.assertEqual(frozen["generation"], GENERATION)
                self.assertEqual(frozen["expires_at"], NOW+120)
                self.assertEqual(frozen["status"], "prepared")
        self.assertEqual(self.raw("PRAGMA user_version"), [(2,)])

    def test_single_result_survives_restart_without_snapshot_or_recalculation(self):
        self.initialize()
        with self.session() as session:
            answer = session.prepare(1, self.snapshot, NOW)
            with patch.object(outbox.private_router, "route_message", side_effect=AssertionError("recalculation")):
                self.assertEqual(session.prepare(1, None, NOW+1), answer)
        with self.session(now=NOW+2) as session:
            with patch.object(outbox.private_router, "route_message", side_effect=AssertionError("recalculation")):
                self.assertEqual(session.prepare(1, {}, NOW+2), answer)
        self.assertEqual(self.raw("SELECT count(*) FROM business_results"), [(1,)])
        self.assertEqual(self.raw("SELECT count(*) FROM outbox"), [(1,)])

    def test_refusals_historical_and_expired_never_read_snapshot(self):
        class Forbidden(dict):
            def get(self, *args):
                raise AssertionError("snapshot read")
        with self.session(create=True, now=NOW-1000) as session:
            refused = message(1)
            refused["message"]["from"]["id"] = 99999
            session.receive_batch([refused, message(2, date=NOW-1100), message(3, date=NOW-121)], 0, NOW)
            with patch.object(outbox.private_router, "route_message", side_effect=AssertionError("routing")):
                for identifier in (1, 2, 3, 999):
                    self.assertIsNone(session.prepare(identifier, Forbidden(), NOW))
        self.assertEqual(self.raw("SELECT count(*) FROM outbox"), [(0,)])

    def test_permission_and_generation_mismatch_fail_before_attempt(self):
        wrong = [None, replace(PERMISSION, enabled=False), replace(PERMISSION, simulation_only=False),
                 replace(PERMISSION, chat_id=-1001234567890), replace(PERMISSION, owner_id=99999),
                 replace(PERMISSION, bot_id=99999), replace(PERMISSION, generation=OTHER_GENERATION)]
        for i, permission in enumerate(wrong):
            self.root = Path(self.temporary.name) / str(i)
            self.initialize(prepared=True)
            transport = outbox.ScriptedTransport()
            with self.session() as session:
                with self.assertRaises(base.StoreError):
                    session.deliver(1, transport, NOW, permission=permission)
            self.assertEqual(transport.requests, [])
            self.assertEqual(self.raw("SELECT count(*) FROM attempts"), [(0,)])
        with self.assertRaises(base.StoreError):
            with self.session(generation=OTHER_GENERATION):
                pass

    def test_altered_destination_response_deadline_or_binding_is_rejected(self):
        modifications = ["UPDATE outbox SET destination=-1001234567890",
                         "UPDATE outbox SET generation='bad'", "UPDATE outbox SET expires_at=expires_at+120",
                         "UPDATE business_results SET response='{}'", "UPDATE business_results SET digest='bad'"]
        for i, sql in enumerate(modifications):
            self.root = Path(self.temporary.name) / str(i)
            self.initialize(prepared=True)
            self.raw(sql)
            with self.assertRaises(base.StoreError):
                with self.session():
                    pass

    def test_success_escaped_html_and_no_retry_even_after_restart(self):
        self.snapshot["pr_snapshots"]["10"]["title"] = '<b>"& café 🦞'
        with self.session(create=True) as session:
            session.receive_batch([message(text="/pr")], 0, NOW)
            response = session.prepare(1, self.snapshot, NOW)
            self.assertIn("&lt;b&gt;", response["text"])
            transport = outbox.ScriptedTransport()
            self.assertEqual(session.deliver(1, transport, NOW, permission=PERMISSION), "confirmed")
            self.assertEqual(session.deliver(1, transport, NOW+2, permission=PERMISSION), "confirmed")
            self.assertEqual(len(transport.accepted), 1)
        with self.session(now=NOW+3) as session:
            self.assertEqual(session.deliver(1, transport, NOW+3, permission=PERMISSION), "confirmed")
        self.assertEqual(len(transport.requests), 1)

    def test_rejected_uncertain_malformed_and_simulated_budget_have_no_retry(self):
        cases = [("reject", 0, "rejected"), ("timeout_after_accept", 10, "uncertain"),
                 ("timeout", 20, "uncertain"), ("success", 21, "uncertain"),
                 ("invalid", 0, "uncertain"), ("wrong_destination", 0, "uncertain"),
                 ("wrong_text", 0, "uncertain"), ("bad_id", 0, "uncertain")]
        for kind, delay, expected in cases:
            self.root = Path(self.temporary.name) / kind
            self.initialize(prepared=True)
            transport = outbox.ScriptedTransport([outbox.SimulatedOutcome(kind, delay)])
            with self.session() as session:
                self.assertEqual(session.deliver(1, transport, NOW, permission=PERMISSION), expected)
            with self.session(now=NOW+25) as session:
                self.assertEqual(session.deliver(1, transport, NOW+25, permission=PERMISSION), expected)
            self.assertEqual(len(transport.requests), 1)

    def test_expiration_boundary_original_deadline_and_cancellation(self):
        for age, expected in ((120, "confirmed"), (120.001, "expired")):
            self.root = Path(self.temporary.name) / str(age)
            self.initialize()
            with self.session(now=NOW+119) as session:
                session.prepare(1, self.snapshot, NOW+119)
                transport = outbox.ScriptedTransport()
                self.assertEqual(session.deliver(1, transport, NOW+age, permission=PERMISSION), expected)
                self.assertEqual(len(transport.requests), int(age == 120))
                self.assertEqual(session.inspect_outbox(1)["expires_at"], NOW+120)
        self.root = Path(self.temporary.name) / "cancel"
        self.initialize(prepared=True)
        with self.session() as session:
            session.cancel(1, NOW)
            session.prepare(1, self.snapshot, NOW)
            self.assertEqual(session.deliver(1, outbox.ScriptedTransport(), NOW, permission=PERMISSION), "cancelled")

    def test_quotas_persist_after_restart_no_catchup_and_exact_minute(self):
        with self.session(create=True) as session:
            session.receive_batch([message(i) for i in range(1, 13)], 0, NOW)
            for i in range(1, 13):
                session.prepare(i, self.snapshot, NOW)
            transport = outbox.ScriptedTransport()
            self.assertEqual(session.deliver(1, transport, NOW, permission=PERMISSION), "confirmed")
        with self.session(now=NOW+1) as session:
            self.assertEqual(session.deliver(2, transport, NOW+1, permission=PERMISSION), "rate_limited")
            for i in range(2, 11):
                self.assertEqual(session.deliver(i, transport, NOW+(i-1)*2, permission=PERMISSION), "confirmed")
            self.assertEqual(session.deliver(11, transport, NOW+20, permission=PERMISSION), "rate_limited")
        with self.session(now=NOW+60) as session:
            self.assertEqual(session.deliver(11, transport, NOW+60, permission=PERMISSION), "confirmed")
            self.assertEqual(session.deliver(12, transport, NOW+60, permission=PERMISSION), "rate_limited")
        self.assertEqual(self.raw("SELECT count(*) FROM attempts"), [(11,)])

    def test_429_cooldown_persists_without_retry_or_resetting_expiration(self):
        with self.session(create=True) as session:
            session.receive_batch([message(1), message(2)], 0, NOW)
            for i in (1, 2):
                session.prepare(i, self.snapshot, NOW)
            transport = outbox.ScriptedTransport([outbox.SimulatedOutcome("reject", 0, 10)])
            self.assertEqual(session.deliver(1, transport, NOW, permission=PERMISSION), "rejected")
        with self.session(now=NOW+2) as session:
            self.assertEqual(session.deliver(2, transport, NOW+2, permission=PERMISSION), "rate_limited")
            self.assertEqual(session.deliver(2, transport, NOW+10, permission=PERMISSION), "confirmed")
        self.assertEqual(len(transport.requests), 2)

    def test_process_crashes_before_and_after_prepare_intent_and_result(self):
        stages = [("prepare", "before_prepare", None), ("prepare", "after_business_result", None),
                  ("prepare", "before_prepare_commit", None),
                  ("prepare", "after_prepare_commit", "prepared"),
                  ("deliver", "before_intent", "prepared"), ("deliver", "before_intent_commit", "prepared"),
                  ("deliver", "after_intent_commit", "uncertain"),
                  ("deliver", "before_result_commit", "uncertain"),
                  ("deliver", "after_result_commit", "confirmed")]
        for phase, stage, expected in stages:
            self.root = Path(self.temporary.name) / stage
            self.initialize(prepared=phase == "deliver")
            child = self.child(phase, stage)
            self.assertEqual(child.returncode, 77, child.stderr)
            with self.session() as session:
                row = session.inspect_outbox(1)
                self.assertEqual(row["status"] if row else None, expected)
                if expected in ("uncertain", "confirmed"):
                    transport = outbox.ScriptedTransport()
                    session.deliver(1, transport, NOW, permission=PERMISSION)
                    self.assertEqual(transport.requests, [])
        # Aucun receipt/cursor B1 n'est modifié par ces crashs B2.
        self.assertEqual(self.raw("SELECT offset FROM epochs"), [(102,)])

    def test_simulated_acceptance_followed_by_commit_error_preserves_intent(self):
        self.initialize(prepared=True)
        def fail(stage):
            if stage == "before_result_commit":
                raise sqlite3.OperationalError("sensitive disk failure")
        transport = outbox.ScriptedTransport()
        with self.session(fault=fail) as session:
            with self.assertRaisesRegex(base.StoreError, "^STORAGE_FAILED$"):
                session.deliver(1, transport, NOW, permission=PERMISSION)
        self.assertEqual(transport.accepted, [1])
        with self.session() as session:
            self.assertEqual(session.inspect_outbox(1)["status"], "uncertain")
        self.assertEqual(self.raw("SELECT count(*) FROM attempts"), [(1,)])

    def test_safe_explicit_migration_preserves_all_b1_data(self):
        with base.SimulationStore(self.root, CONFIG, BOT, NOW, create=True) as session:
            session.receive_batch([message()], 0, NOW)
        before = {table: self.raw("SELECT * FROM " + table) for table in base.SCHEMA}
        with self.assertRaisesRegex(base.StoreError, "MIGRATION_REQUIRED"):
            with self.session():
                pass
        self.assertEqual(self.raw("PRAGMA user_version"), [(1,)])
        with self.session(migrate=True) as session:
            self.assertEqual(session.status()["stored_offset"], 102)
            for table, rows in before.items():
                self.assertEqual(self.raw("SELECT * FROM " + table), rows)
            session.prepare(1, self.snapshot, NOW)
        with self.assertRaisesRegex(base.StoreError, "SCHEMA_INVALID"):
            with base.SimulationStore(self.root, CONFIG, BOT, NOW):
                pass  # B1 ne doit jamais rétrograder ou écraser B2.

    def test_migration_crashes_rollback_or_leave_complete_v2(self):
        for stage in ("before_migration", "during_migration", "before_migration_commit", "after_migration_commit"):
            self.root = Path(self.temporary.name) / stage
            with base.SimulationStore(self.root, CONFIG, BOT, NOW, create=True) as session:
                session.receive_batch([message()], 0, NOW)
            child = self.child("migration", stage)
            self.assertEqual(child.returncode, 77, child.stderr)
            expected = 2 if stage == "after_migration_commit" else 1
            self.assertEqual(self.raw("PRAGMA user_version"), [(expected,)])
            with self.session(migrate=True) as session:
                self.assertEqual(session.status()["messages"], 1)
                session.prepare(1, self.snapshot, NOW)

    def test_migration_refuses_incompatible_schema_without_modification(self):
        self.initialize()
        self.raw("PRAGMA user_version=99")
        before = (self.root / "fixtures.sqlite3").read_bytes()
        with self.assertRaises(base.StoreError):
            with self.session(migrate=True):
                pass
        self.assertEqual((self.root / "fixtures.sqlite3").read_bytes(), before)

    def test_full_storage_cannot_leave_partial_business_result(self):
        with self.session(create=True) as session:
            session.receive_batch([message()], 0, NOW)
            size = session.connection.execute("PRAGMA page_count").fetchone()[0]*session.connection.execute("PRAGMA page_size").fetchone()[0]
            session.limits = (100000, 1000, size-1)
            with self.assertRaises(base.StoreError):
                session.prepare(1, self.snapshot, NOW)
        self.assertEqual(self.raw("SELECT count(*) FROM outbox"), [(0,)])
        self.assertEqual(self.raw("SELECT count(*) FROM business_results"), [(0,)])

    def test_real_sqlite_full_rolls_back_only_failed_preparation(self):
        with self.session(create=True) as session:
            session.receive_batch([message(i, "/help") for i in range(1, 21)], 0, NOW)
            pages = session.connection.execute("PRAGMA page_count").fetchone()[0]
            session.connection.execute(f"PRAGMA max_page_count={pages}")
            committed = 0
            for identifier in range(1, 21):
                try:
                    session.prepare(identifier, self.snapshot, NOW)
                    committed += 1
                except base.StoreError as error:
                    self.assertEqual(error.__context__.sqlite_errorcode, sqlite3.SQLITE_FULL)
                    break
            else:
                self.fail("fixture must reach SQLITE_FULL")
        self.assertEqual(self.raw("SELECT count(*) FROM outbox"), [(committed,)])
        self.assertEqual(self.raw("SELECT count(*) FROM business_results"), [(committed,)])
        self.assertEqual(self.raw("SELECT count(*) FROM attempts"), [(0,)])

    def test_no_io_environment_process_network_or_input_mutation_during_delivery(self):
        self.initialize()
        before = copy.deepcopy(self.snapshot)
        transport = outbox.ScriptedTransport()
        with self.session() as session, ExitStack() as stack:
            for target in ("builtins.open", "pathlib.Path.open", "subprocess.Popen", "socket.socket",
                           "urllib.request.urlopen", "requests.sessions.Session.request", "os.getenv",
                           "os._Environ.__getitem__", "time.time", "time.sleep"):
                stack.enter_context(patch(target, side_effect=AssertionError("forbidden")))
            session.prepare(1, self.snapshot, NOW)
            session.deliver(1, transport, NOW, permission=PERMISSION)
        self.assertEqual(self.snapshot, before)
        self.assertEqual(transport.accepted, [1])

    def test_randomized_reopen_prepare_and_delivery_never_duplicate(self):
        rng = random.Random(8232)
        with self.session(create=True) as session:
            session.receive_batch([message(i) for i in range(1, 11)], 0, NOW)
        transport = outbox.ScriptedTransport()
        for index in range(35):
            now = NOW+index*2
            with self.session(now=now) as session:
                identifier = rng.randint(1, 10)
                session.prepare(identifier, self.snapshot, now)
                session.deliver(identifier, transport, now, permission=PERMISSION)
        self.assertEqual(len(transport.accepted), len(set(transport.accepted)))
        self.assertEqual(self.raw("SELECT count(*) FROM attempts")[0][0], len(transport.accepted))


if __name__ == "__main__":
    unittest.main()

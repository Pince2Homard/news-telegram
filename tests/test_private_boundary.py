"""Frontière : ACL/filesystem simulés ; SQLite réel sur fixtures uniquement.

Aucune lecture de Q, aucun Get-Acl, aucune mutation de permissions Windows.
"""
from contextlib import ExitStack
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.windows_watcher import private_boundary as boundary
from tools.windows_watcher import private_store as base, private_recovery as recovery
from tools.windows_watcher import private_trial as trial, private_stop as stop
from tests import test_private_outbox as fixtures
from tests import test_private_qualification as qualification_fixtures
from tests import test_private_recovery as recovery_fixtures
from tests.test_private_outbox import CONFIG, BOT, NOW, message
from tests.test_private_f3 import E, ModelKernel


USER = "S-1-5-21-111111111-222222222-333333333-1001"
FOREIGN = "S-1-5-21-444444444-555555555-666666666-1002"
VOLUME = Path(Path.cwd().anchor)
VIRTUAL = VOLUME / "synthetic-private-boundary"
PUBLICATION = VOLUME / "synthetic-publication"


class Filesystem:
    """Métadonnées uniquement, jamais de création de ces chemins virtuels."""
    def __init__(self):
        self.paths = {role: VIRTUAL / role for role in sorted(boundary.ROLES)}
        self.stats, self.children, self.overrides = {}, {}, {}
        for root in self.paths.values():
            self.add(root)

    def add(self, path, *, directory=True):
        for node in reversed((path, *path.parents)):
            if node not in self.stats:
                self.stats[node] = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700,
                    st_dev=1, st_ino=len(self.stats)+1, st_nlink=1, st_file_attributes=0)
                self.children[node] = []
                if node.parent != node:
                    self.children[node.parent].append(node)
        self.stats[path].st_mode = (stat.S_IFDIR if directory else stat.S_IFREG) | 0o600

    def inspect(self, paths):
        rows = []
        for path in paths:
            row = dict(path=str(path), owner=USER, protected=True, null_dacl=False,
                reparse=False, rules=[dict(sid=USER, allow=True, rights=2032127, inherit_only=False)])
            row.update(self.overrides.get(path, {}))
            rows.append(row)
        return dict(user=USER, rows=rows)

    def patches(self):
        stack = ExitStack()
        stack.enter_context(patch.object(boundary, "_windows", return_value=True))
        stack.enter_context(patch.object(Path, "lstat", autospec=True,
            side_effect=lambda path: self.stats[path]))
        stack.enter_context(patch.object(Path, "iterdir", autospec=True,
            side_effect=lambda path: iter(self.children[path])))
        stack.enter_context(patch.object(boundary.credential_acl, "inspect", side_effect=self.inspect))
        return stack

    def boundary(self, **changes):
        options = dict(paths=self.paths, approval_reference="issue-43-approved-fixtures",
                       publication_root=PUBLICATION)
        options.update(changes)
        return boundary.PrivateStorageBoundary(**options)


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fs = Filesystem()

    def test_explicit_complete_approval_and_absolute_nonoverlapping_paths(self):
        for changes in (dict(approval_reference=""), dict(approval_reference="secret/path"),
                dict(paths={}), dict(paths={**self.fs.paths, "store": Path("relative")}),
                dict(paths={**self.fs.paths, "store": self.fs.paths["fence"] / "nested"})):
            with self.subTest(changes=changes), patch.object(Path, "lstat", side_effect=AssertionError("no disk")):
                with self.assertRaises(boundary.BoundaryError):self.fs.boundary(**changes)

    def test_forbidden_paths_fail_lexically_without_reading_q_or_acl(self):
        forbidden = [VOLUME / "NewsTelegramPrivate-Qualification-synthetic-Q" / "store",
            VOLUME / "Temp" / "store", VOLUME / "workspace" / ".codex" / "store",
            boundary.WORKSPACE / "store", PUBLICATION / "store", PUBLICATION.parent,
            VOLUME / "telegram-once", VOLUME / "credentials"]
        with patch.object(Path, "lstat", side_effect=AssertionError("no disk")), \
                patch.object(boundary.credential_acl, "inspect", side_effect=AssertionError("no ACL")):
            for root in forbidden:
                with self.subTest(root=root), self.assertRaises(boundary.BoundaryError):
                    self.fs.boundary(paths={**self.fs.paths, "store": root})

    def test_ambiguous_windows_names_are_refused_without_filesystem_probe(self):
        for name in ("Temp.", "Temp ", "NEWSQA~1", "store:stream", "NUL", "store?", "store\x00"):
            with self.subTest(name=name), patch.object(Path, "lstat", side_effect=AssertionError("no disk")):
                with self.assertRaises(boundary.BoundaryError):
                    self.fs.boundary(paths={**self.fs.paths, "store": VOLUME / name})

    def test_positive_model_and_config_copy_no_automatic_creation(self):
        with self.fs.patches():
            guard = self.fs.boundary()
            self.fs.paths["store"] = VIRTUAL / "changed"
            self.assertEqual(guard.paths["store"], VIRTUAL / "store")
            guard.verify(store=guard.paths["store"])
            with patch.object(Path, "mkdir", side_effect=AssertionError("no provisioning")):
                with self.assertRaisesRegex(boundary.BoundaryError, "PROVISIONING"):
                    boundary.check(guard, create=True, store=guard.paths["store"])

    def test_wrong_role_or_location_closes_without_fallback(self):
        for locations in (dict(store=VOLUME / "elsewhere"), dict(unknown=VIRTUAL),
                          dict(store=self.fs.paths["store"] / "nested")):
            with self.subTest(locations=locations), self.fs.patches():
                guard = self.fs.boundary()
                with self.assertRaisesRegex(boundary.BoundaryError, "UNSAFE"):guard.verify(**locations)
                with self.assertRaisesRegex(boundary.BoundaryError, "CLOSED"):guard.verify()

    def test_existing_control_descendant_is_inspected(self):
        child = self.fs.paths["control"] / "operation"
        self.fs.add(child)
        with self.fs.patches():
            guard = self.fs.boundary()
            guard.verify(control=child)
            self.fs.overrides[child] = dict(null_dacl=True)
            with self.assertRaises(boundary.BoundaryError):guard.verify(control=child)

    def test_any_extra_group_including_codex_is_refused_on_each_role_and_annex(self):
        for role in boundary.ROLES:
            for annex in (False, True):
                fs = Filesystem()
                target = fs.paths[role]
                if annex:
                    target = target / "fixture.sqlite3-journal"
                    fs.add(target, directory=False)
                with self.subTest(role=role, annex=annex), fs.patches():
                    guard = fs.boundary()
                    fs.overrides[target] = dict(rules=[dict(sid=FOREIGN,
                        name="CodexSandboxUsers", allow=True, rights=1179817, inherit_only=False)])
                    with self.assertRaises(boundary.BoundaryError):guard.verify()
                    fs.overrides.clear()
                    with self.assertRaisesRegex(boundary.BoundaryError, "CLOSED"):guard.verify()

    def test_dangerous_parent_rights_are_rejected_without_changing_them(self):
        with self.fs.patches():
            guard = self.fs.boundary()
            self.fs.overrides[VIRTUAL] = dict(rules=[dict(sid=FOREIGN, allow=True,
                rights=0x40000, inherit_only=False)])
            with self.assertRaises(boundary.BoundaryError):guard.verify()

    def test_inherit_only_extra_group_is_rejected_before_future_files_exist(self):
        with self.fs.patches():
            guard = self.fs.boundary()
            self.fs.overrides[self.fs.paths["store"]] = dict(rules=[dict(sid=FOREIGN,
                allow=True, rights=1179817, inherit_only=True)])
            with self.assertRaises(boundary.BoundaryError):guard.verify()

    def test_principal_change_is_not_an_implicit_reenrollment(self):
        with self.fs.patches():
            guard = self.fs.boundary()
            def changed(paths):
                value = self.fs.inspect(paths)
                value["user"] = FOREIGN
                for row in value["rows"]:row["owner"], row["rules"] = FOREIGN, []
                return value
            with patch.object(boundary.credential_acl, "inspect", side_effect=changed):
                with self.assertRaises(boundary.BoundaryError):guard.verify()

    def test_owner_null_dacl_reparse_inheritance_and_incomplete_proof(self):
        for change in (dict(owner=FOREIGN), dict(null_dacl=True), dict(reparse=True), dict(protected=False)):
            with self.subTest(change=change), self.fs.patches():
                guard = self.fs.boundary()
                self.fs.overrides[self.fs.paths["witness"]] = change
                with self.assertRaises(boundary.BoundaryError):guard.verify()
                self.fs.overrides.clear()
        for result in (dict(user=USER, rows=[]), PermissionError("synthetic sensitive path")):
            with self.fs.patches():
                guard = self.fs.boundary()
                kwargs = dict(side_effect=result) if isinstance(result, Exception) else dict(return_value=result)
                with patch.object(boundary.credential_acl, "inspect", **kwargs):
                    with self.assertRaisesRegex(boundary.BoundaryError, "^PRIVATE_STORAGE_UNSAFE$"):guard.verify()

    def test_replaced_directory_missing_file_reparse_hardlink_and_quota(self):
        for failure in ("replacement", "missing", "reparse", "hardlink", "quota"):
            fs = Filesystem()
            annex = fs.paths["store"] / "fixture.sqlite3-journal"
            fs.add(annex, directory=False)
            with self.subTest(failure=failure), fs.patches():
                guard = fs.boundary()
                if failure == "replacement":fs.stats[fs.paths["store"]].st_ino += 100
                elif failure == "missing":del fs.stats[annex]
                elif failure == "reparse":fs.stats[annex].st_file_attributes = 0x400
                elif failure == "hardlink":fs.stats[annex].st_nlink = 2
                else:
                    for number in range(boundary.MAX_NODES):fs.add(fs.paths["control"] / str(number))
                with self.assertRaises(boundary.BoundaryError):guard.verify()

    def test_windows_required_and_partial_or_different_wiring_is_refused(self):
        with patch.object(boundary, "_windows", return_value=False), \
                patch.object(Path, "lstat", side_effect=AssertionError("no disk")):
            with self.assertRaises(boundary.BoundaryError):self.fs.boundary()
        with self.fs.patches():
            guard, other = self.fs.boundary(), self.fs.boundary()
        a = SimpleNamespace(boundary=guard)
        self.assertIs(boundary.shared(a, a), guard)
        for b in (SimpleNamespace(), SimpleNamespace(boundary=other)):
            with self.assertRaises(boundary.BoundaryError):boundary.shared(a, b)
        self.assertIsNone(boundary.shared(SimpleNamespace(), SimpleNamespace()))

    def test_mismatched_startup_is_rejected_before_fixture_path_probe_or_sql(self):
        constructors = (
            lambda guard: base.SimulationStore(VOLUME / "wrong", CONFIG, BOT, NOW, boundary=guard),
            lambda guard: recovery.FixtureFence(VOLUME / "wrong", self.fs.paths["witness"],
                self.fs.paths["store"] / "fixtures.sqlite3", {}, NOW, boundary=guard),
            lambda guard: trial.FixtureTrial(VOLUME / "wrong", E, boundary=guard))
        for constructor in constructors:
            with self.fs.patches():guard = self.fs.boundary()
            with patch.object(base, "_fixture_directory", side_effect=AssertionError("no path probe")), \
                    patch.object(base.sqlite3, "connect", side_effect=AssertionError("no SQL")):
                with self.assertRaises(boundary.BoundaryError):constructor(guard)

    def test_stop_handles_remain_usable_when_disk_boundary_is_closed(self):
        with self.fs.patches():guard = self.fs.boundary()
        guard.failed = True
        gate = stop.NativeStopGate.__new__(stop.NativeStopGate)
        gate.boundary, gate.path = guard, self.fs.paths["control"] / "stop-ticket.json"
        gate.api, gate.handle, gate.job = ModelKernel(), 1, None
        with self.assertRaises(boundary.BoundaryError):gate.check()
        gate.close()
        self.assertTrue(gate.api.stopped)
        self.assertIsNone(gate.handle)
        authorization = trial.FixtureTrial.__new__(trial.FixtureTrial)
        authorization.job = SimpleNamespace(safe=False, stop=Mock())
        authorization.db, authorization.failed, authorization.data = object(), False, {"state": "active"}
        authorization._save = Mock(side_effect=boundary.BoundaryError("PRIVATE_BOUNDARY_CLOSED"))
        with self.assertRaises(boundary.BoundaryError):authorization.suspend()
        authorization.job.stop.assert_called_once_with()

    def test_partial_adapter_wiring_refuses_before_provider_or_fixture_write(self):
        from tools.windows_watcher import private_bot_adapter as api, private_credential as credential
        from tests.test_private_f3 import TARGET
        with self.fs.patches():guard = self.fs.boundary()
        fence = recovery.FixtureFence.__new__(recovery.FixtureFence)
        fence.boundary = guard
        authorization = trial.FixtureTrial.__new__(trial.FixtureTrial)
        authorization.boundary, authorization.enrollment = None, E
        adapter = api.LocalBotAdapter(E, TARGET, api.LocalEndpoint(1), enabled=True)
        provider = credential.PinnedSyntheticProvider(TARGET)
        with patch.object(provider, "acquire") as acquire, patch.object(adapter, "write_fixture") as write:
            with self.assertRaisesRegex(api.TransportRefused, "REFUSED_BEFORE_SEND"):
                adapter.execute(self.fs.paths["control"], adapter.prepare("getMe", {}),
                                fence, authorization, provider)
            acquire.assert_not_called()
            write.assert_not_called()
            self.assertFalse(provider.enabled)


class BoundaryTransactions(unittest.TestCase):
    """Réutilisation des fixtures B2, sans répéter toute leur suite."""
    setUp = fixtures.PrivateOutboxTests.setUp
    session = fixtures.PrivateOutboxTests.session
    initialize = fixtures.PrivateOutboxTests.initialize

    def test_acl_veto_during_receipt_commit_rolls_back_cursor_and_receipts(self):
        fs = Filesystem()
        with fs.patches():guard = fs.boundary()
        with self.session(create=True) as store:
            # Le chemin est une fixture Temp historique. Seul le veto est simulé,
            # jamais une permission native ni un contournement de Q.
            store.boundary = guard
            calls = []
            def verify(**locations):
                calls.append(locations)
                if store.connection.in_transaction:
                    raise boundary.BoundaryError("PRIVATE_STORAGE_UNSAFE")
            with patch.object(guard, "verify", side_effect=verify):
                with self.assertRaises((base.StoreError, boundary.BoundaryError)):
                    store.receive_batch([message()], 0, NOW)
            store.boundary = None
            self.assertTrue(calls)
            self.assertTrue(all(value == {"store": store.root} for value in calls))
            self.assertEqual(store.status()["receipts"], 0)
            self.assertEqual(store.status()["next_offset"], 0)

    def test_acl_veto_preserves_uncertain_intent_and_no_second_send(self):
        from tools.windows_watcher import private_outbox as outbox
        from tests.test_private_outbox import PERMISSION
        self.initialize(prepared=True)
        fs = Filesystem()
        with fs.patches():guard = fs.boundary()
        transport = outbox.ScriptedTransport([outbox.SimulatedOutcome("timeout_after_accept")])
        with self.session() as store:
            self.assertEqual(store.deliver(1, transport, NOW, permission=PERMISSION), "uncertain")
            before = store.path.read_bytes()
            store.boundary = guard
            with self.assertRaises(boundary.BoundaryError):store.inspect_outbox(1)
            self.assertEqual(store.path.read_bytes(), before)
        with self.session() as store:
            self.assertEqual(store.deliver(1, transport, NOW+1, permission=PERMISSION), "uncertain")
            self.assertEqual(len(transport.requests), 1)
            self.assertEqual(store.status()["receipts"], 1)


class BoundaryRecovery(unittest.TestCase):
    def test_veto_cannot_seal_a_dirty_generation_or_reset_its_journals(self):
        case = recovery_fixtures.FixtureTests("runTest")
        self.addCleanup(case.doCleanups)
        case.setUp()
        with case.fence() as fence:
            session = fence.begin()
            fence.touch_store()
            before = [(root / "fence.sqlite3").read_bytes() for root in (fence.root, fence.witness)]
            fs = Filesystem()
            with fs.patches():fence.boundary = fs.boundary()
            with self.assertRaises(boundary.BoundaryError):fence.seal()
            self.assertTrue(fence.state["dirty"])
            self.assertEqual(fence.state["session"], session)
            self.assertEqual(before, [(root / "fence.sqlite3").read_bytes() for root in (fence.root, fence.witness)])
        with case.fence() as reopened:
            with self.assertRaisesRegex(recovery.FenceError, "PREVIOUS_SESSION_UNPROVEN"):reopened.begin()

    def test_restoration_barrier_survives_veto_and_no_guarded_backup_is_provisioned(self):
        from tools.windows_watcher import private_qualification as q
        case = qualification_fixtures.QualificationTests("runTest")
        self.addCleanup(case.doCleanups)
        case.setUp()
        case.initialize()
        backup, restored = case.area.reserve("backup"), case.area.reserve("restored")
        with case.session() as store:store.backup_fixture(backup, NOW)
        q.restore_fixture(case.area, backup, restored, CONFIG, BOT, NOW,
            generation=qualification_fixtures.GENERATION, source_id=qualification_fixtures.SOURCE,
            store_id=qualification_fixtures.RESTORED)
        with q.QualifiedStore(restored, CONFIG, BOT, NOW, area=case.area,
                store_id=qualification_fixtures.RESTORED,
                generation=qualification_fixtures.GENERATION) as store:
            fs = Filesystem()
            with fs.patches():store.boundary = fs.boundary()
            before = store.path.read_bytes()
            with patch.object(store, "_qualification", side_effect=AssertionError("no read before ACL")):
                with self.assertRaises(boundary.BoundaryError):store._guard(NOW)
            self.assertEqual(store.path.read_bytes(), before)
            with patch.object(store.boundary, "verify"), patch.object(case.area, "prepare_new") as provision:
                with self.assertRaisesRegex(boundary.BoundaryError, "PRIVATE_BACKUP_NOT_APPROVED"):
                    store.backup_fixture(case.area.reserve("unapproved"), NOW)
                provision.assert_not_called()
            store.boundary = None
            with self.assertRaisesRegex(base.StoreError, "RESTORE_BARRIER"):store._guard(NOW)

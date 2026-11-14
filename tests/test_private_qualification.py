"""C0 : fixtures isolées. Windows réel activé explicitement sur l'aire approuvée."""
from contextlib import ExitStack, closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from tools.windows_watcher import private_qualification as q, private_store as base
from tools.windows_watcher.private_outbox import ScriptedTransport, SimulationPermission


NOW = 1800000000
BOT = 54321
GENERATION = "11111111-1111-4111-8111-111111111111"
SOURCE = "33333333-3333-4333-8333-333333333333"
RESTORED = "44444444-4444-4444-8444-444444444444"
CONFIG = dict(mode="simulation", send_enabled=False, notifications_channel_id=None,
              owner_user_id=12345, arbitration_private_chat_id=12345)
PERMISSION = SimulationPermission(BOT, 12345, 12345, GENERATION, True)
WORKSPACE = Path(__file__).resolve().parents[1]


def update():
    return {"update_id": 100, "message": {"message_id": 1, "date": NOW, "text": "/status",
            "from": {"id": 12345, "is_bot": False}, "chat": {"id": 12345, "type": "private"}}}


def synthetic_acl(paths):
    """Métadonnées synthétiques pour les tests fonctionnels ; aucune preuve Windows."""
    user = "S-1-5-21-111111111-222222222-333333333-1001"
    return {"user": user, "rows": [{"path": str(path), "owner": user,
        "protected": True, "null_dacl": False, "reparse": False,
        "rules": [{"sid": user, "allow": True, "rights": 2032127, "inherit_only": False}]}
        for path in paths]}


CHILD = r'''
import os,sys
from pathlib import Path
from unittest.mock import patch
from tools.windows_watcher import private_qualification as q
from tools.windows_watcher.private_outbox import ScriptedTransport,SimulationPermission
root, phase, stage = sys.argv[1:]
if os.name == "nt":
    # Ce processus vérifie les transactions, pas les ACL natives.
    def synthetic(paths):
        user="S-1-5-21-111111111-222222222-333333333-1001"
        return {"user":user,"rows":[dict(path=str(p),owner=user,protected=True,null_dacl=False,
            reparse=False,rules=[dict(sid=user,allow=True,rights=2032127,inherit_only=False)]) for p in paths]}
    q.credential_acl.inspect=synthetic
    q._powershell=lambda *args:None
config=dict(mode="simulation",send_enabled=False,notifications_channel_id=None,
            owner_user_id=12345,arbitration_private_chat_id=12345)
generation="11111111-1111-4111-8111-111111111111"
identity="33333333-3333-4333-8333-333333333333"
def crash(point):
    if point == stage:
        os._exit(77)
with q.QualificationArea(root=root) as area:
    with q.QualifiedStore(area.reserve('source'),config,54321,1800000000,area=area,
            store_id=identity,generation=generation,fault=crash) as session:
        if phase=='backup':
            session.backup_fixture(area.reserve('backup-crash'),1800000000)
        else:
            session.deliver(1,ScriptedTransport(),1800000000,
                permission=SimulationPermission(54321,12345,12345,generation,True))
'''


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        if os.name == "nt":
            self.stack.enter_context(patch.object(q, "_powershell"))
            self.stack.enter_context(patch.object(q.credential_acl, "inspect", side_effect=synthetic_acl))
        self.area = self.stack.enter_context(q.QualificationArea(parent=WORKSPACE))
        self.source = self.area.reserve("source")

    def session(self, **kwargs):
        return q.QualifiedStore(self.source, CONFIG, BOT, kwargs.pop("now", NOW), area=self.area,
            store_id=kwargs.pop("store_id", SOURCE), generation=kwargs.pop("generation", GENERATION), **kwargs)

    def initialize(self):
        with self.session(create=True) as session:
            session.receive_batch([update()], 0, NOW)
            session.prepare(1, None, NOW)

    def raw(self, path, sql):
        with closing(sqlite3.connect(path / "fixtures.sqlite3")) as connection:
            result = connection.execute(sql).fetchall()
            connection.commit()
            return result

    def restore(self, backup, name="restored", now=NOW):
        path = self.area.reserve(name)
        result = q.restore_fixture(self.area, backup, path, CONFIG, BOT, now,
            generation=GENERATION, source_id=SOURCE, store_id=RESTORED)
        return path, result

    def child(self, phase, stage):
        return subprocess.run([sys.executable, "-c", CHILD, str(self.area.root), phase, stage],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_coherent_backup_preserves_receipts_cursor_and_disarms_old_image(self):
        self.initialize()
        backup = self.area.reserve("backup")
        with self.session() as session:
            before = session.status()
            session.backup_fixture(backup, NOW)
            self.assertEqual(session.status(), before)
            transport = ScriptedTransport()
            self.assertEqual(session.deliver(1, transport, NOW, permission=PERMISSION), "confirmed")
        restored, status = self.restore(backup)
        self.assertTrue(status["restored_disarmed"])
        if os.name != "nt":
            for path in restored.iterdir():
                self.assertEqual(path.stat().st_mode & 0o077,0)
        self.assertIsNone(status["next_offset"])
        self.assertEqual(status["stored_offset"], before["stored_offset"])
        with q.QualifiedStore(restored, CONFIG, BOT, NOW, area=self.area, store_id=RESTORED,
                              generation=GENERATION) as session:
            self.assertEqual(session.inspect_outbox(1)["status"], "cancelled")
            with self.assertRaises(base.StoreError):
                session.deliver(1, transport, NOW, permission=PERMISSION)
        self.assertEqual(transport.accepted, [1])
        self.assertEqual(self.raw(restored,"SELECT count(*) FROM receipts"), [(1,)])
        self.assertEqual(self.raw(restored,"SELECT count(*) FROM messages"), [(1,)])

    def test_restoration_cannot_rearm_via_epoch_or_other_reader(self):
        self.initialize()
        backup = self.area.reserve("backup")
        with self.session() as session:
            session.backup_fixture(backup, NOW)
        restored, _ = self.restore(backup, now=NOW+3*86400)
        with q.QualifiedStore(restored, CONFIG, BOT, NOW+3*86400, area=self.area,
                store_id=RESTORED,generation=GENERATION) as session:
            with self.assertRaisesRegex(base.StoreError, "RESTORE_BARRIER"):
                session.recover_epoch([update()],NOW+3*86400,approval="reviewed-simulation-recovery")
            with self.assertRaisesRegex(base.StoreError, "RESTORE_BARRIER"):
                session.receive_batch([update()],101,NOW+3*86400)
        with self.assertRaises(base.StoreError):
            with q.delivery.DeliveryStore(restored,CONFIG,BOT,NOW+3*86400,generation=GENERATION):
                pass

    def test_backup_refuses_open_transaction_and_existing_destination(self):
        self.initialize()
        backup = self.area.reserve("backup")
        with self.session() as session:
            with session._transaction():
                with self.assertRaises(base.StoreError):
                    session.backup_fixture(backup,NOW)
            self.assertFalse(backup.exists())
            session.backup_fixture(backup,NOW)
            before=(backup/"image.sqlite3").read_bytes()
            with self.assertRaises(FileExistsError):
                session.backup_fixture(backup,NOW)
            self.assertEqual((backup/"image.sqlite3").read_bytes(),before)

    def test_retention_stops_at_two_images_without_eviction(self):
        self.initialize()
        with self.session() as session:
            for name in ("backup-one","backup-two"):
                session.backup_fixture(self.area.reserve(name),NOW)
            third=self.area.reserve("backup-three")
            with self.assertRaisesRegex(base.StoreError,"BACKUP_QUOTA"):
                session.backup_fixture(third,NOW)
            self.assertFalse(third.exists())

    def test_corrupted_backup_and_identity_mismatch_are_refused_without_overwrite(self):
        self.initialize()
        backup=self.area.reserve("backup")
        with self.session() as session:
            session.backup_fixture(backup,NOW)
        destination=self.area.reserve("restored")
        with self.assertRaises(base.StoreError):
            q.restore_fixture(self.area,backup,destination,CONFIG,BOT,NOW,generation=GENERATION,
                              source_id=RESTORED,store_id=SOURCE)
        self.assertFalse(destination.exists())
        annex=backup/"image.sqlite3-wal"
        with open(annex,"xb",opener=lambda path,flags:os.open(path,flags,0o600)) as output:
            output.write(b"unexpected fixture annex")
        with self.assertRaisesRegex(base.StoreError,"BACKUP_INVALID"):
            self.restore(backup)
        self.assertFalse(destination.exists())
        annex.unlink()  # Annexe nouvelle de ce test uniquement.
        with (backup/"image.sqlite3").open("ab") as output:
            output.write(b"corrupt")
        with self.assertRaisesRegex(base.StoreError,"BACKUP_INVALID"):
            self.restore(backup)
        self.assertFalse(destination.exists())

    def test_identity_generation_clock_and_corruption_stop_safely(self):
        self.initialize()
        with self.assertRaises(base.StoreError):
            with self.session(store_id=RESTORED):
                pass
        with self.assertRaises(base.StoreError):
            with self.session(generation=RESTORED):
                pass
        with self.session(now=NOW-1) as session:
            self.assertEqual(session.status()["state"],"recovery_required")
            with self.assertRaises(base.StoreError):
                session.deliver(1,ScriptedTransport(),NOW-1,permission=PERMISSION)
        path=self.source/"fixtures.sqlite3"
        path.write_bytes(b"corrupt")
        before=path.read_bytes()
        with self.assertRaises(base.StoreError):
            with self.session():
                pass
        self.assertEqual(path.read_bytes(),before)

    def test_interrupted_send_remains_uncertain_and_can_be_backed_up(self):
        self.initialize()
        child=self.child("deliver","after_intent_commit")
        self.assertEqual(child.returncode,77,child.stderr)
        backup=self.area.reserve("backup")
        with self.session() as session:
            self.assertEqual(session.inspect_outbox(1)["status"],"uncertain")
            session.backup_fixture(backup,NOW)
        restored,_=self.restore(backup)
        self.assertEqual(self.raw(restored,"SELECT status FROM attempts"),[("uncertain",)])
        self.assertEqual(self.raw(restored,"SELECT status FROM outbox"),[("uncertain",)])

    def test_crash_during_backup_or_before_manifest_never_publishes_partial_image(self):
        self.initialize()
        for stage in ("during_backup","before_backup_manifest"):
            child=self.child("backup",stage)
            self.assertEqual(child.returncode,77,child.stderr)
            backup=self.area.reserve("backup-crash")
            self.assertFalse((backup/"manifest.json").exists())
            with self.assertRaises(base.StoreError):
                self.restore(backup)
            # Retirer seulement les fichiers de cette tentative de fixture connue.
            for path in backup.iterdir():
                base._safe_path(path)
                self.assertIn(path.name,{"image.sqlite3","image.sqlite3-journal"})
                path.unlink()
            backup.rmdir()
        with self.session() as session:
            self.assertEqual(session.status()["stored_offset"],101)
            self.assertEqual(session.inspect_outbox(1)["status"],"prepared")

    def test_cleanup_refuses_unknown_inventory_and_marker_changes(self):
        original=(self.area.root/"c0-area.json").read_text(encoding="utf-8")
        extra=self.area.root/"unknown-fixture"
        extra.write_text("fixture",encoding="utf-8")
        with self.assertRaisesRegex(base.StoreError,"CLEANUP_REFUSED"):
            self.area.cleanup()
        self.assertTrue(extra.exists())
        extra.unlink()  # Uniquement le fichier créé dans ce test.
        (self.area.root/"c0-area.json").write_text("{}",encoding="utf-8")
        with self.assertRaisesRegex(base.StoreError,"CLEANUP_REFUSED"):
            self.area.cleanup()
        self.assertTrue(self.area.root.exists())
        (self.area.root/"c0-area.json").write_text(original,encoding="utf-8")

    def test_area_refuses_unapproved_paths_nonempty_preparation_and_reuse(self):
        with self.assertRaises(base.StoreError):
            self.area.authorize(WORKSPACE)
        self.initialize()
        with self.assertRaises(base.StoreError):
            self.area._prepare_empty(self.source)
        with self.assertRaises(base.StoreError):
            with self.session(create=True):
                pass

    def test_deadline_workers_are_interrupted_without_network_for_all_stages(self):
        for stage in ("dns","tls","read","write"):
            with patch("socket.socket",side_effect=AssertionError("no network")):
                result=q.qualify_deadline_fixture(stage,0.5)
            self.assertTrue(result["interrupted"])
            self.assertTrue(result["stage_entered"])
            self.assertLess(result["elapsed"],1.7)

    @unittest.skipUnless(os.name=="nt","Windows synthetic ACL cases")
    def test_acl_kernel_refuses_owner_inheritance_rights_and_annexes(self):
        self.initialize()
        for kind in ("owner","inheritance","rights","annex"):
            def unsafe(paths):
                result=synthetic_acl(paths)
                row=result["rows"][0]
                if kind=="owner":row["owner"]="S-1-5-21-999-999-999-1001"
                elif kind=="inheritance":row["protected"]=False
                else:row["rules"].append(dict(sid="S-1-1-0",allow=True,rights=2032127,inherit_only=False))
                return result
            with patch.object(q.credential_acl,"inspect",side_effect=unsafe):
                with self.assertRaisesRegex(base.StoreError,"ACL_UNSAFE"):
                    self.area.verify(self.source)


@unittest.skipUnless(os.name=="nt" and os.environ.get("C0_APPROVED_WINDOWS_FIXTURE")=="1",
                     "Explicit native Windows fixture authorization required")
class NativeWindowsQualificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.area=q.QualificationArea.approved_windows_fixture()

    @classmethod
    def tearDownClass(cls):
        cls.area.cleanup()
        print("C0 native Windows: approved fixture identity/inventory checked; cleanup completed")

    def test_actual_private_directory_and_sqlite_journal(self):
        source=self.area.reserve("native-source")
        with q.QualifiedStore(source,CONFIG,BOT,NOW,area=self.area,store_id=SOURCE,
                              generation=GENERATION,create=True) as session:
            session.receive_batch([update()],0,NOW)
            with session._transaction():
                session.connection.execute("UPDATE epochs SET reason='native-fixture'")
                self.assertTrue((source/"fixtures.sqlite3-journal").is_file())
                proof=self.area.verify(source)
                self.assertTrue(proof["acl_verified"])
                self.assertGreaterEqual(proof["targets"],5)
            backup=self.area.reserve("native-backup")
            session.prepare(1,None,NOW)
            session.backup_fixture(backup,NOW)
        restored=self.area.reserve("native-restored")
        result=q.restore_fixture(self.area,backup,restored,CONFIG,BOT,NOW,
            generation=GENERATION,source_id=SOURCE,store_id=RESTORED)
        self.assertTrue(result["restored_disarmed"])
        self.assertTrue(self.area.verify(restored)["acl_verified"])
        print("C0 native Windows: private owner/DACL/inheritance/parents/SQLite annexes verified; disarmed restore passed")

    def test_actual_foreign_read_permission_is_refused(self):
        root=self.area.prepare_new(self.area.reserve("native-negative"))
        path=root/"fixtures.sqlite3"
        path.write_text("synthetic fixture",encoding="utf-8")
        identity=self.area._inode(path)
        script=r'''
$ErrorActionPreference='Stop'
$path=[Console]::In.ReadToEnd()
$acl=[System.Security.AccessControl.FileSecurity]::new($path,[System.Security.AccessControl.AccessControlSections]::Access)
$sid=[System.Security.Principal.SecurityIdentifier]::new('S-1-1-0')
$acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($sid,'Read','Allow'))
[System.IO.File]::SetAccessControl($path,$acl)
'''
        q._powershell(script,str(path))
        with self.assertRaisesRegex(base.StoreError,"ACL_UNSAFE"):
            self.area.verify(root)
        self.assertEqual(self.area._inode(path),identity)
        path.unlink()  # Seulement le fichier négatif créé et identifié dans ce test.
        self.assertTrue(self.area.verify(root)["acl_verified"])

    def test_actual_reenabled_inheritance_is_refused(self):
        root=self.area.prepare_new(self.area.reserve("native-inheritance"))
        identity=self.area._inode(root)
        script=r'''
$path=[Console]::In.ReadToEnd()
$acl=[System.Security.AccessControl.DirectorySecurity]::new($path,[System.Security.AccessControl.AccessControlSections]::Access)
$acl.SetAccessRuleProtection($false,$true)
[System.IO.Directory]::SetAccessControl($path,$acl)
'''
        q._powershell(script,str(root))
        with self.assertRaisesRegex(base.StoreError,"ACL_UNSAFE"):
            self.area.verify(root)
        self.assertEqual(self.area._inode(root),identity)
        self.assertEqual(list(root.iterdir()),[])
        root.rmdir()  # Nouveau sous-dossier de test, vide et identifié.


if __name__=="__main__":
    unittest.main()

"""Sondes Windows stdlib, exécutables seules sous un runtime sans dépendances.

Les définitions testées viennent du dépôt, par une liste AST fermée. Ce n'est
pas un import complet des composants ni une qualification de leur intégration.
"""
import ast
import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest


def definitions(filename, names, **extra):
    root=Path(__file__).resolve().parents[1]/"tools/windows_watcher"
    tree=ast.parse((root/filename).read_text(encoding="utf-8"))
    selected=[node for node in tree.body if isinstance(node,(ast.ClassDef,ast.FunctionDef)) and node.name in names]
    if {node.name for node in selected}!=names:raise AssertionError("PROBE_SOURCE_CHANGED")
    namespace=dict(ctypes=ctypes,w=w,os=os,re=re,**extra)
    exec(compile(ast.Module(body=selected,type_ignores=[]),filename,"exec"),namespace)
    return namespace


@unittest.skipUnless(os.name=="nt","Native Windows runtime prerequisite probes")
class NativeRuntimePrerequisites(unittest.TestCase):
    def test_ctypes_layout_job_limits_and_noninheritable_local_duplicate(self):
        source=definitions("private_job.py",{"WorkerError","UnsafeStop","BasicLimit","IOCount",
            "ExtendedLimit","Accounting","Startup","ProcessInfo","_api"})
        extended=definitions("private_fenced_job.py",{"StartupEx"},jobs=SimpleNamespace(Startup=source["Startup"]))
        for name,size in (("Startup",104),("ExtendedLimit",144),("ProcessInfo",24)):
            self.assertEqual(ctypes.sizeof(source[name]),size)
        self.assertEqual(ctypes.sizeof(extended["StartupEx"]),112)
        self.assertEqual(ctypes.sizeof(ctypes.c_void_p),8)
        kernel=source["_api"]();job=None;duplicate=w.HANDLE()
        kernel.GetCurrentProcess.argtypes,kernel.GetCurrentProcess.restype=[],w.HANDLE
        kernel.DuplicateHandle.argtypes,kernel.DuplicateHandle.restype=[w.HANDLE,w.HANDLE,w.HANDLE,ctypes.POINTER(w.HANDLE),w.DWORD,w.BOOL,w.DWORD],w.BOOL
        kernel.GetHandleInformation.argtypes,kernel.GetHandleInformation.restype=[w.HANDLE,ctypes.POINTER(w.DWORD)],w.BOOL
        try:
            job=kernel.CreateJobObjectW(None,None);self.assertTrue(job)
            limits=source["ExtendedLimit"]();limits.basic.flags=0x2000|8;limits.basic.active_limit=8
            self.assertTrue(kernel.SetInformationJobObject(job,9,ctypes.byref(limits),ctypes.sizeof(limits)))
            effective=source["ExtendedLimit"]()
            self.assertTrue(kernel.QueryInformationJobObject(job,9,ctypes.byref(effective),ctypes.sizeof(effective),None))
            self.assertTrue(effective.basic.flags&0x2000)
            self.assertEqual(effective.basic.active_limit,8)
            self.assertTrue(kernel.DuplicateHandle(kernel.GetCurrentProcess(),job,kernel.GetCurrentProcess(),ctypes.byref(duplicate),0,False,2))
            flags=w.DWORD()
            self.assertTrue(kernel.GetHandleInformation(duplicate,ctypes.byref(flags)))
            self.assertFalse(flags.value&1)
            counts=source["Accounting"]()
            self.assertTrue(kernel.QueryInformationJobObject(job,1,ctypes.byref(counts),ctypes.sizeof(counts),None))
            self.assertEqual(counts.active,0)
            self.assertTrue(kernel.TerminateJobObject(job,79))
        finally:
            for handle in (duplicate,job):
                if handle:self.assertTrue(kernel.CloseHandle(handle))

    def test_dpapi_source_functions_synthetic_only_and_buffer_cleanup(self):
        source=definitions("private_credential.py",{"CredentialError","wipe","synthetic","Blob","_dpapi","SyntheticVault"})
        plaintext=bytearray(b"C3-SYNTHETIC-"+b"a"*32)
        vault=source["SyntheticVault"](plaintext)
        try:
            self.assertFalse(any(plaintext))
            self.assertTrue(vault.use(lambda value:value==bytearray(b"C3-SYNTHETIC-"+b"a"*32)))
        finally:vault.close()
        self.assertFalse(any(vault.blob))
        with self.assertRaises(source["CredentialError"]):vault.use(lambda value:None)

    def test_sqlite_effective_durability_rollback_and_reopen(self):
        # Qualification du moteur seulement : ni schéma complet B1 ni crash OS.
        with tempfile.TemporaryDirectory(prefix="phase-f4b1-runtime-") as temp:
            path=Path(temp)/"synthetic.sqlite3"
            db=sqlite3.connect(path,isolation_level=None,timeout=.1)
            try:
                for pragma in ("journal_mode=DELETE","synchronous=EXTRA","foreign_keys=ON",
                        "temp_store=MEMORY","trusted_schema=OFF","busy_timeout=100"):
                    db.execute("PRAGMA "+pragma)
                expected=dict(journal_mode="delete",synchronous=3,foreign_keys=1,temp_store=2,trusted_schema=0,busy_timeout=100)
                self.assertEqual({key:db.execute("PRAGMA "+key).fetchone()[0] for key in expected},expected)
                db.execute("CREATE TABLE receipts (id INTEGER PRIMARY KEY)")
                db.execute("CREATE TABLE cursor (offset INTEGER)");db.execute("INSERT INTO cursor VALUES(0)")
                db.execute("BEGIN IMMEDIATE");db.execute("INSERT INTO receipts VALUES(10)")
                db.execute("UPDATE cursor SET offset=11");db.execute("ROLLBACK")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0],0)
                self.assertEqual(db.execute("SELECT offset FROM cursor").fetchone()[0],0)
                db.execute("BEGIN IMMEDIATE");db.execute("INSERT INTO receipts VALUES(10)")
                db.execute("UPDATE cursor SET offset=11");db.execute("COMMIT")
            finally:db.close()
            db=sqlite3.connect(path)
            try:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0],"ok")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0],1)
                self.assertEqual(db.execute("SELECT offset FROM cursor").fetchone()[0],11)
            finally:db.close()


if __name__=="__main__":unittest.main()

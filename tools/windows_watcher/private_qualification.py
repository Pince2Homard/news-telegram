"""C0 hors ligne : ACL et sauvegardes de fixtures exclusivement, aucun transport."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

from . import coordination, credential_acl, private_outbox as delivery, private_store as base


PREPARE_ACL = r'''
$ErrorActionPreference = 'Stop'
try {
  Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
  $path = [Console]::In.ReadToEnd()
  $item = Get-Item -LiteralPath $path -Force
  if (-not $item.PSIsContainer -or ($item.Attributes -band 1024) -ne 0) { exit 2 }
  if (@(Get-ChildItem -LiteralPath $path -Force).Count -ne 0) { exit 2 }
  $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
  $acl = Get-Acl -LiteralPath $path
  $owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
  if ($owner -notin @($user.Value, 'S-1-5-18', 'S-1-5-32-544')) { exit 2 }
  # Ne charger/persister que la DACL : aucun changement d'owner ou de SACL.
  $acl = [System.Security.AccessControl.DirectorySecurity]::new(
    $path, [System.Security.AccessControl.AccessControlSections]::Access)
  $acl.SetAccessRuleProtection($true, $false)
  foreach ($rule in @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))) {
    $acl.RemoveAccessRuleSpecific($rule)
  }
  foreach ($sid in @($user.Value, 'S-1-5-18', 'S-1-5-32-544')) {
    $identity = New-Object System.Security.Principal.SecurityIdentifier($sid)
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
      $identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
  }
  [System.IO.Directory]::SetAccessControl($path, $acl)
} catch { exit 2 }
'''
QUALIFICATION_SCHEMA = {"qualification": "CREATE TABLE qualification (id INTEGER PRIMARY KEY CHECK(id=1), store_id TEXT NOT NULL, restored INTEGER NOT NULL CHECK(restored IN (0,1)), parent_id TEXT)"}
SCHEMA = {**delivery.SCHEMA, **QUALIFICATION_SCHEMA}
VERSION = 3


def _powershell(script, stdin):
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-Command", script],
                            input=stdin, text=True, capture_output=True, timeout=20,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise base.StoreError("ACL_UNSAFE")


def _write_json(path, value):
    with open(path, "x", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600)) as output:
        json.dump(value, output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())


def _hash(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


class QualificationArea:
    """Seuls les enfants directs nommés d'un temporaire C0 sont autorisés.

    root=None crée l'aire. root existant est destiné aux processus de tests
    explicitement rattachés à cette même aire, jamais à une installation réelle.
    """

    def __init__(self, root=None, *, parent=None):
        self.owned = root is None
        self.container = None
        self.allowed = set()
        self.children = {}
        if root is None:
            if parent is None:
                raise base.StoreError("FIXTURE_PARENT_REQUIRED")
            parent = base._fixture_directory(parent)
            self.area_id = str(uuid4())
            self.root = parent / ("phase823-c0-" + self.area_id)
            if self.root.exists():
                raise base.StoreError("FIXTURE_EXISTS")
            self.root.mkdir(mode=0o700, exist_ok=False)
            self._prepare_empty(self.root)
            _write_json(self.root / "c0-area.json", self._marker())
        else:
            self.root = base._fixture_directory(root)
            marker = self.root / "c0-area.json"
            base._safe_path(marker)
            if (not self.root.name.startswith("phase823-c0-") or not marker.is_file()
                    or marker.stat().st_size > 256):
                raise base.StoreError("FIXTURE_REQUIRED")
            saved = json.loads(marker.read_text(encoding="utf-8"))
            self.area_id = saved.get("area_id")
            if not coordination.uuid_text(self.area_id) or saved != self._marker():
                raise base.StoreError("FIXTURE_REQUIRED")
            self.allowed = {child for child in self.root.iterdir() if child.is_dir()
                            and re.fullmatch(r"[a-z][a-z0-9-]{0,40}", child.name)}
        self.identity = self._inode(self.root)
        self.verify(self.root)

    def _marker(self):
        return {"fixtures_only": True, "format": 1, "area_id": self.area_id}

    @staticmethod
    def _inode(path):
        info = path.lstat()
        return info.st_dev, info.st_ino

    @classmethod
    def approved_windows_fixture(cls):
        """Emplacement unique explicitement approuvé ; aucun dossier existant repris."""
        if os.name != "nt":
            raise base.StoreError("WINDOWS_REQUIRED")
        container = base._fixture_directory(Path("C:/phase823-C0-"))
        if container.exists():
            raise base.StoreError("FIXTURE_EXISTS")
        container.mkdir(mode=0o700, exist_ok=False)
        # Seul ce nouveau conteneur vide est protégé ; jamais son parent C:\.
        _powershell(PREPARE_ACL, str(container))
        area = cls(parent=container)
        area.container = container
        area.container_identity = cls._inode(container)
        return area

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.owned:
            self.cleanup()

    def cleanup(self):
        """Inventaire fermé + identité + ACL avant toute suppression récursive."""
        if not self.owned:
            raise base.StoreError("CLEANUP_REFUSED")
        try:
            base._safe_path(self.root)
            if self._inode(self.root) != self.identity:
                raise ValueError("identity")
            self.verify(self.root)
            if json.loads((self.root / "c0-area.json").read_text(encoding="utf-8")) != self._marker():
                raise ValueError("marker")
            names = {"fixtures.sqlite3", "fixtures.sqlite3-journal", "fixture.lock", "fixture-identity.json",
                     "image.sqlite3", "image.sqlite3-journal", "manifest.json"}
            for child in self.root.iterdir():
                base._safe_path(child)
                if child.name == "c0-area.json" and child.is_file():
                    continue
                if child not in self.allowed or not child.is_dir():
                    raise ValueError("inventory")
                if self._inode(child) != self.children.get(child):
                    raise ValueError("child identity")
                if any(not item.is_file() or item.name not in names for item in child.iterdir()):
                    raise ValueError("inventory")
                self.verify(child)
            self.verify(self.root)
            if self.container is not None:
                base._safe_path(self.container)
                if (self._inode(self.container) != self.container_identity
                        or set(self.container.iterdir()) != {self.root}):
                    raise ValueError("container")
        except Exception:
            raise base.StoreError("CLEANUP_REFUSED") from None
        shutil.rmtree(self.root)
        if self.container is not None:
            self.container.rmdir()
        self.owned = False

    def reserve(self, name):
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", name):
            raise base.StoreError("FIXTURE_REQUIRED")
        path = self.root / name
        base._safe_path(path)
        self.allowed.add(path)
        return path

    def authorize(self, path):
        path = Path(path).absolute()
        if path not in self.allowed or path.parent != self.root:
            raise base.StoreError("FIXTURE_REQUIRED")
        base._safe_path(path)
        return path

    def _prepare_empty(self, path):
        path = Path(path).absolute()
        if path != self.root:
            self.authorize(path)
        if not path.is_dir() or any(path.iterdir()):
            raise base.StoreError("FIXTURE_NOT_EMPTY")
        base._safe_path(path)
        if os.name == "nt":
            _powershell(PREPARE_ACL, str(path))
        elif path.stat().st_mode & 0o077:
            raise base.StoreError("ACL_UNSAFE")
        self.verify(path)
        if path != self.root:
            self.children[path] = self._inode(path)

    def prepare_new(self, path):
        path = self.authorize(path)
        path.mkdir(mode=0o700, exist_ok=False)
        self._prepare_empty(path)
        return path

    def verify(self, path):
        """Inspection groupée, règles du vérificateur existant inchangées."""
        path = Path(path).absolute()
        if path != self.root and path not in getattr(self, "allowed", set()):
            raise base.StoreError("FIXTURE_REQUIRED")
        base._safe_path(path)
        targets = [path]
        if path.is_dir():
            # Pas de parcours récursif de liens ou de dossiers non approuvés.
            for child in path.iterdir():
                base._safe_path(child)
                if child.is_file():
                    targets.append(child)
                elif child.parent != self.root or child not in getattr(self, "allowed", set()):
                    raise base.StoreError("FILES_UNSAFE")
        if len(targets) > 16:
            raise base.StoreError("QUOTA_EXCEEDED")
        for target in targets:
            if target.is_file() and target.stat().st_nlink != 1:
                raise base.StoreError("FILES_UNSAFE")
        try:
            if os.name == "nt":
                chains = {target: [target, *target.parents] for target in targets}
                all_paths = list(dict.fromkeys(node for nodes in chains.values() for node in nodes))
                snapshot = credential_acl.inspect(all_paths)
                rows = {os.path.normcase(row["path"]): row for row in snapshot["rows"]}
                if rows[os.path.normcase(str(path))].get("protected") is not True:
                    raise ValueError("directory inheritance")
                for target, nodes in chains.items():
                    credential_acl.validate({"user": snapshot["user"], "rows": [
                        rows[os.path.normcase(str(node))] for node in nodes]}, nodes)
            else:
                if any(target.stat().st_mode & 0o077 for target in targets):
                    raise ValueError("file modes")
        except Exception:
            raise base.StoreError("ACL_UNSAFE") from None
        return {"fixtures_only": True, "acl_verified": True, "targets": len(targets)}


class QualifiedStore(delivery.DeliveryStore):
    """Schéma C0 avec identité injectée et barrière irréversible de restauration."""

    def __init__(self, directory, *args, area, store_id, **kwargs):
        if type(area) is not QualificationArea or not coordination.uuid_text(store_id):
            raise base.StoreError("IDENTITY_INVALID")
        self.area = area
        self.store_id = store_id
        directory = area.authorize(directory)
        if directory.exists():
            area.verify(directory)
        super().__init__(directory, *args, **kwargs)

    def _on_directory_created(self):
        self.area._prepare_empty(self.area.authorize(self.root))

    def _check_files(self):
        super()._check_files()
        self.area.verify(self.root)

    def _validate(self):
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version in (1, 2):
            if not (self.create or self.migrate):
                raise base.StoreError("MIGRATION_REQUIRED")
            delivery.DeliveryStore._validate(self)
            with self._transaction():
                self.connection.execute(QUALIFICATION_SCHEMA["qualification"])
                self.connection.execute("INSERT INTO qualification VALUES(?, ?, 0, NULL)", (1, self.store_id))
                self.connection.execute(f"PRAGMA user_version={VERSION}")
                self.fault("before_qualification_commit")
        base.SimulationStore._validate(self, schema=SCHEMA, version=VERSION)
        self._validate_delivery()
        row = self._qualification()
        if row["restored"] and self._meta()["state"] != "recovery_required":
            raise base.StoreError("RESTORE_BARRIER")

    def _qualification(self):
        row = self.connection.execute("SELECT * FROM qualification WHERE id=1").fetchone()
        if (row is None or row["store_id"] != self.store_id
                or (row["parent_id"] is not None and not coordination.uuid_text(row["parent_id"]))):
            raise base.StoreError("IDENTITY_INVALID")
        return row

    def _guard(self, now, permission=None):
        if self._qualification()["restored"]:
            raise base.StoreError("RESTORE_BARRIER")
        return super()._guard(now, permission)

    def recover_epoch(self, *args, **kwargs):
        self._active()
        if self._qualification()["restored"]:
            raise base.StoreError("RESTORE_BARRIER")
        return super().recover_epoch(*args, **kwargs)

    def _receive(self, *args, **kwargs):
        self._active()
        if self._qualification()["restored"]:
            raise base.StoreError("RESTORE_BARRIER")
        return super()._receive(*args, **kwargs)

    def backup_fixture(self, destination, now):
        """Image cohérente, toujours désarmée, dans un nouveau dossier de fixture."""
        self._active()
        self._check_files()
        self._validate()
        now = base._time(now)
        if self.connection.in_transaction or now < self._meta()["last_clock"]:
            raise base.StoreError("BACKUP_REFUSED")
        images = [path for path in self.area.allowed if (path / "image.sqlite3").is_file()]
        if len(images) >= 2:
            raise base.StoreError("BACKUP_QUOTA")
        destination = self.area.prepare_new(destination)
        image = destination / "image.sqlite3"
        descriptor = os.open(image, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        try:
            with closing(sqlite3.connect(image, isolation_level=None)) as target:
                self.connection.backup(target, pages=64, sleep=0,
                    progress=lambda *args: self.fault("during_backup"))
                _disarm(target, now)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise base.StoreError("BACKUP_INVALID")
            self.fault("before_backup_manifest")
            _write_json(destination / "manifest.json", {"format": 1, "schema": VERSION,
                "source_id": self.store_id, "generation": self.generation, "disarmed": True,
                "sha256": _hash(image)})
            self.area.verify(destination)
            return {"fixtures_only": True, "disarmed": True}
        except Exception:
            # Une image partielle n'est jamais publiée par un manifeste ; pas de reset.
            raise base.StoreError("BACKUP_FAILED") from None


def _disarm(connection, now):
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=EXTRA")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("BEGIN IMMEDIATE")
    try:
        meta = json.loads(connection.execute("SELECT data FROM meta WHERE id=1").fetchone()[0])
        meta.update(state="recovery_required", reason="INTERNAL_FAILURE", last_clock=max(now, meta["last_clock"]))
        connection.execute("UPDATE meta SET data=? WHERE id=1", (json.dumps(meta),))
        connection.execute("UPDATE qualification SET restored=1 WHERE id=1")
        connection.execute("UPDATE attempts SET status='uncertain',completed_at=MAX(?,started_at),code='restart' WHERE status='in_flight'", (meta["last_clock"],))
        connection.execute("UPDATE outbox SET status='uncertain' WHERE status='in_flight'")
        connection.execute("UPDATE outbox SET status='cancelled' WHERE status='prepared'")
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def restore_fixture(area, backup, destination, config, bot_id, now, *, generation, source_id, store_id):
    """Aucun écrasement, aucune réactivation ; les anciennes images restent bloquées."""
    backup = area.authorize(backup)
    destination = area.authorize(destination)
    area.verify(backup)
    if (not coordination.uuid_text(source_id) or not coordination.uuid_text(store_id)
            or source_id == store_id or not coordination.uuid_text(generation)):
        raise base.StoreError("IDENTITY_INVALID")
    now = base._time(now)
    manifest_path, image = backup / "manifest.json", backup / "image.sqlite3"
    if (set(path.name for path in backup.iterdir()) != {"manifest.json", "image.sqlite3"}
            or not manifest_path.is_file() or manifest_path.stat().st_size > 1024
            or not image.is_file() or image.stat().st_size > base.MAX_DATABASE_BYTES):
        raise base.StoreError("BACKUP_INVALID")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = {"format": 1, "schema": VERSION, "source_id": source_id,
                "generation": generation, "disarmed": True, "sha256": _hash(image)}
    if manifest != expected:
        raise base.StoreError("BACKUP_INVALID")
    area.prepare_new(destination)
    target_path = destination / "fixtures.sqlite3"
    descriptor = os.open(target_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(image.as_uri()+"?mode=ro", uri=True)) as source, closing(
                sqlite3.connect(target_path, isolation_level=None)) as target:
            source.backup(target, pages=64, sleep=0)
            row = target.execute("SELECT store_id,restored FROM qualification WHERE id=1").fetchone()
            if row != (source_id, 1):
                raise base.StoreError("BACKUP_INVALID")
            _disarm(target, now)
            target.execute("BEGIN IMMEDIATE")
            target.execute("UPDATE qualification SET store_id=?,parent_id=? WHERE id=1", (store_id, source_id))
            target.execute("COMMIT")
        config = delivery.supervision.validate_config(config)
        _write_json(destination / "fixture-identity.json", {"simulation_only": True, "bot_id": bot_id,
            "owner_id": config["owner_user_id"], "chat_id": config["arbitration_private_chat_id"]})
        with QualifiedStore(destination, config, bot_id, now, area=area, store_id=store_id,
                            generation=generation) as restored:
            return {**restored.status(), "restored_disarmed": True}
    except Exception:
        raise base.StoreError("RESTORE_FAILED") from None


def qualify_deadline_fixture(stage, budget=1.0):
    """Worker bloqué artificiellement, jamais DNS/TLS/socket ou transport réel."""
    if stage not in {"dns", "tls", "read", "write"} or type(budget) not in (int, float) or not 0.1 <= budget <= 2:
        raise base.StoreError("BUDGET_INVALID")
    script = "import sys,time; print(sys.argv[1],flush=True); time.sleep(60)"
    started = time.monotonic()
    with subprocess.Popen([sys.executable, "-c", script, stage], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as worker:
        try:
            worker.communicate(timeout=max(0.001, budget-(time.monotonic()-started)))
            raise base.StoreError("BUDGET_INVALID")
        except subprocess.TimeoutExpired:
            worker.kill()
            output, _ = worker.communicate(timeout=1)
        return {"fixtures_only": True, "interrupted": worker.returncode is not None,
                "stage_entered": output.decode().strip() == stage,
                "elapsed": time.monotonic()-started, "stage": stage}

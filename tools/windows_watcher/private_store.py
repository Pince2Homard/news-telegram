"""B1 : magasin de fixtures privé, sans transport, routage ou outbox.

Toutes les heures et identités sont injectées. Aucun emplacement de production
ou credential n'est découvert. Un magasin existant ne se recrée jamais.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3

from . import credential_acl, private_router, supervision, watcher


SCHEMA_VERSION = 1
MAX_BATCH = 20
MAX_RECEIPTS = 100000
MAX_PENDING = 1000
MAX_DATABASE_BYTES = 128 * 1024 * 1024
MAX_DIRECTORY_BYTES = 512 * 1024 * 1024
REASONS = frozenset({
    "initial", "clock_reversed", "epoch_review", "retention_gap", "manual_epoch",
    "BATCH_INVALID", "BATCH_ORDER", "CLOCK_FUTURE", "CURSOR_MISMATCH", "RECOVERY_INVALID",
    "RECOVERY_REQUIRED", "UPDATE_CONFLICT", "UPDATE_REGRESSION", "MESSAGE_CONFLICT",
    "QUOTA_EXCEEDED", "PATH_UNSAFE", "FILES_UNSAFE", "STORAGE_FAILED", "INTERNAL_FAILURE",
})
SCHEMA = {
    "meta": "CREATE TABLE meta (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)",
    "epochs": "CREATE TABLE epochs (id INTEGER PRIMARY KEY, offset INTEGER NOT NULL CHECK(offset>=0), reason TEXT NOT NULL)",
    "messages": "CREATE TABLE messages (message_id INTEGER PRIMARY KEY CHECK(message_id>0), digest TEXT NOT NULL, payload TEXT, date INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','expired','historical')))",
    "receipts": "CREATE TABLE receipts (epoch INTEGER NOT NULL REFERENCES epochs(id), update_id INTEGER NOT NULL CHECK(update_id>=0), digest TEXT, message_id INTEGER REFERENCES messages(message_id), disposition TEXT NOT NULL CHECK(disposition IN ('admitted','expired','historical','duplicate_message','rejected')), received_at REAL NOT NULL, PRIMARY KEY(epoch,update_id))",
}


class StoreError(RuntimeError):
    """Code fermé, jamais de chemin, SQL, texte ou exception sous-jacente."""


def _time(value):
    if type(value) not in (int, float) or not 0 <= value < 2**52 or not math.isfinite(value):
        raise StoreError("CLOCK_INVALID")
    return float(value)


def _positive(value):
    return type(value) is int and 0 < value < 2**52


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _safe_path(path):
    # Pas de lien de réanalyse sur le fichier, dossier ou chaîne des parents.
    path = Path(path).absolute()
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise StoreError("PATH_UNSAFE")
        if candidate.exists():
            info = candidate.lstat()
            if getattr(info, "st_file_attributes", 0) & 0x400:
                raise StoreError("PATH_UNSAFE")
    return path


def _fixture_directory(path):
    path = Path(path).absolute()
    # B1 ne peut pas cibler les emplacements nommés de supervision réelle.
    if any(part.casefold() in {"newstelegramwatcher", "telegram-once", "credentials", ".git"}
           for part in path.parts):
        raise StoreError("PATH_UNSAFE")
    return _safe_path(path)


class SimulationStore:
    """Session mono-instance sur un répertoire de test explicite exclusivement.

    create=True initialise un NOUVEAU dossier ; create=False exige DB et marqueur.
    fault est un point d'injection de panne pour tests, jamais un transport.
    Les limites sont abaissables pour simulations ; aucune option de mode réel.
    """

    def __init__(self, directory, config, bot_id, now, *, create=False,
                 receipt_limit=MAX_RECEIPTS, pending_limit=MAX_PENDING,
                 database_bytes=MAX_DATABASE_BYTES, fault=None):
        if type(create) is not bool:
            raise StoreError("MODE_INVALID")
        try:
            self.config = supervision.validate_config(config)
        except (ValueError, TypeError):
            raise StoreError("IDENTITY_INVALID") from None
        if (not _positive(bot_id) or self.config["owner_user_id"] is None
                or self.config["arbitration_private_chat_id"] is None):
            raise StoreError("IDENTITY_INVALID")
        for value, maximum in ((receipt_limit, MAX_RECEIPTS), (pending_limit, MAX_PENDING),
                               (database_bytes, MAX_DATABASE_BYTES)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise StoreError("QUOTA_INVALID")
        self.root = _fixture_directory(directory)
        self.path = self.root / "fixtures.sqlite3"
        self.marker = self.root / "fixture-identity.json"
        self.binding = {"simulation_only": True, "bot_id": bot_id,
                        "owner_id": self.config["owner_user_id"],
                        "chat_id": self.config["arbitration_private_chat_id"]}
        self.now = _time(now)
        self.create = create
        self.limits = (receipt_limit, pending_limit, database_bytes)
        self.fault = fault or (lambda stage: None)
        self.connection = None
        self.lock = None
        self.failed = False

    def __enter__(self):
        try:
            if self.create:
                # Un dossier préexistant, même vide, ne devient pas un nouveau magasin.
                self.root.mkdir(mode=0o700, parents=False, exist_ok=False)
                # ACL héritée du dossier sous Windows ; modes privés sous POSIX.
                for path in (self.path, self.root / "fixture.lock"):
                    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    os.close(descriptor)
            elif not self.root.is_dir():
                raise StoreError("STORE_MISSING")
            self.lock = watcher.single_instance_lock(self.root / "fixture.lock")
            self.lock.__enter__()
            if self.create:
                with open(self.marker, "x", encoding="utf-8",
                          opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
                    json.dump(self.binding, output)
                    output.flush()
                    os.fsync(output.fileno())
                # Le marqueur reste après une création interrompue : aucun reset implicite.
            else:
                self._check_files()
                if not self.path.is_file() or not self.marker.is_file():
                    raise StoreError("STORE_MISSING")
                if self.marker.stat().st_size > 1024:
                    raise StoreError("IDENTITY_INVALID")
                if json.loads(self.marker.read_text(encoding="utf-8")) != self.binding:
                    raise StoreError("IDENTITY_INVALID")
            self._check_files()
            uri = self.path.as_uri() + ("?mode=rwc" if self.create else "?mode=rw")
            self.connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=0.1)
            self.connection.row_factory = sqlite3.Row
            if not self.create and self.connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise StoreError("SQLITE_MODE")
            self.connection.execute("PRAGMA journal_mode=DELETE")
            self.connection.execute("PRAGMA synchronous=EXTRA")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA temp_store=MEMORY")
            self.connection.execute("PRAGMA trusted_schema=OFF")
            self.connection.execute("PRAGMA busy_timeout=100")
            if self.settings() != {"journal_mode": "delete", "synchronous": 3,
                                   "foreign_keys": 1, "temp_store": 2, "trusted_schema": 0,
                                   "busy_timeout": 100}:
                raise StoreError("SQLITE_MODE")
            if self.create:
                with self._transaction():
                    for sql in SCHEMA.values():
                        self.connection.execute(sql)
                    self.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    self.connection.execute("INSERT INTO epochs VALUES(0,0,'initial')")
                    meta = {"binding": self.binding, "epoch": 0, "state": "normal", "reason": "initial",
                            "last_receive": self.now, "last_update": self.now,
                            "last_clock": self.now, "activated_at": self.now}
                    self.connection.execute("INSERT INTO meta VALUES(1,?)", (json.dumps(meta),))
            self._validate()
            self._quota()
            self.resume(self.now)
            self._on_open()
            return self
        except BaseException as error:
            self.__exit__(None, None, None)
            if isinstance(error, (KeyboardInterrupt, SystemExit, StoreError)):
                raise
            code = "LOCKED" if isinstance(error, watcher.WatcherError) else "STORE_UNAVAILABLE"
            raise StoreError(code) from None

    def __exit__(self, *args):
        try:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
        finally:
            if self.lock is not None:
                self.lock.__exit__(None, None, None)
                self.lock = None

    def _active(self):
        if self.connection is None or self.failed:
            raise StoreError("SESSION_CLOSED")

    def _on_open(self):
        """Point d'extension de reprise, sans effet pour le schéma B1."""

    @contextmanager
    def _transaction(self):
        self._active()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def _check_files(self):
        _safe_path(self.root)
        if os.name != "nt" and self.root.stat().st_mode & 0o077:
            raise StoreError("FILES_UNSAFE")
        allowed = {"fixtures.sqlite3", "fixtures.sqlite3-journal", "fixture.lock", "fixture-identity.json"}
        total = 0
        for path in self.root.iterdir():
            _safe_path(path)
            if path.name not in allowed or not path.is_file() or path.stat().st_nlink != 1:
                raise StoreError("FILES_UNSAFE")
            size = path.stat().st_size
            total += size
            if path == self.path and size > self.limits[2]:
                raise StoreError("QUOTA_EXCEEDED")
            if os.name != "nt" and path.stat().st_mode & 0o077:
                # La simulation ne promet aucune ACL Windows ; sous POSIX refuser
                # les fichiers ouverts aux autres comptes, sans corriger leurs droits.
                raise StoreError("FILES_UNSAFE")
        if total > MAX_DIRECTORY_BYTES:
            raise StoreError("QUOTA_EXCEEDED")

    def settings(self):
        self._active()
        return {key: self.connection.execute("PRAGMA " + key).fetchone()[0]
                for key in ("journal_mode", "synchronous", "foreign_keys", "temp_store",
                            "trusted_schema", "busy_timeout")}

    def _meta(self):
        return json.loads(self.connection.execute("SELECT data FROM meta WHERE id=1").fetchone()[0])

    def _save_meta(self, meta):
        self.connection.execute("UPDATE meta SET data=? WHERE id=1", (json.dumps(meta),))

    def _validate(self, *, schema=SCHEMA, version=SCHEMA_VERSION):
        if self.connection.execute("PRAGMA user_version").fetchone()[0] != version:
            raise StoreError("SCHEMA_INVALID")
        actual = dict(self.connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
        if actual != schema or self.connection.execute("PRAGMA integrity_check").fetchall()[0][0] != "ok":
            raise StoreError("SCHEMA_INVALID")
        if self.connection.execute("PRAGMA foreign_key_check").fetchone():
            raise StoreError("INTEGRITY_INVALID")
        meta = self._meta()
        if (not isinstance(meta, dict) or set(meta) != {"binding", "epoch", "state", "reason",
                "last_receive", "last_update", "last_clock", "activated_at"}
                or type(meta["epoch"]) is not int):
            raise StoreError("SCHEMA_INVALID")
        if (meta["binding"] != self.binding or meta["state"] not in
                {"normal", "gap_possible", "recovery_required"}
                or not isinstance(meta["reason"], str) or meta["reason"] not in REASONS):
            raise StoreError("IDENTITY_INVALID")
        for key in ("last_receive", "last_update", "last_clock", "activated_at"):
            _time(meta[key])
        if not meta["activated_at"] <= meta["last_update"] <= meta["last_receive"] <= meta["last_clock"]:
            raise StoreError("CLOCK_INVALID")
        self._quota()
        epochs = self.connection.execute("SELECT id,offset FROM epochs ORDER BY id").fetchall()
        if not epochs or [row[0] for row in epochs] != list(range(len(epochs))) or meta["epoch"] != len(epochs)-1:
            raise StoreError("CURSOR_INVALID")
        for epoch, offset in epochs:
            maximum = self.connection.execute("SELECT MAX(update_id) FROM receipts WHERE epoch=?", (epoch,)).fetchone()[0]
            if offset != (maximum + 1 if maximum is not None else 0):
                raise StoreError("CURSOR_INVALID")
        for row in self.connection.execute("SELECT message_id,digest,payload,date,status FROM messages"):
            if row[2] is None or _digest(json.loads(row[2])) != row[1]:
                raise StoreError("INTEGRITY_INVALID")
            payload = json.loads(row[2])
            if (not _positive(row[0]) or payload["message_id"] != row[0] or payload["date"] != row[3]
                    or not _positive(payload["message_id"]) or type(payload["date"]) is not int
                    or not isinstance(payload.get("text"), str)
                    or not 0 < len(payload["text"]) <= private_router.MAX_COMMAND_LENGTH
                    or not private_router.authenticate(payload, self.config)):
                raise StoreError("INTEGRITY_INVALID")
        if self.connection.execute("""SELECT 1 FROM receipts r LEFT JOIN messages m
                ON r.message_id=m.message_id WHERE
                (r.disposition='rejected' AND (r.message_id IS NOT NULL OR r.digest IS NOT NULL))
                OR (r.disposition!='rejected' AND (m.message_id IS NULL OR r.digest IS NULL OR r.digest!=m.digest))
                LIMIT 1""").fetchone():
            raise StoreError("INTEGRITY_INVALID")

    def _quota(self):
        receipts, pending, size = self.limits
        if (self.connection.execute("SELECT count(*) FROM receipts").fetchone()[0] > receipts
                or self.connection.execute("SELECT count(*) FROM messages WHERE status='pending'").fetchone()[0] > pending
                or self.connection.execute("PRAGMA page_count").fetchone()[0] *
                self.connection.execute("PRAGMA page_size").fetchone()[0] > size
                or sum(path.stat().st_size for path in self.root.iterdir()) > MAX_DIRECTORY_BYTES):
            raise StoreError("QUOTA_EXCEEDED")

    def _suspend(self, code):
        if self.failed:
            return
        try:
            with self._transaction():
                meta = self._meta()
                meta.update(state="recovery_required", reason=code)
                self._save_meta(meta)
        except sqlite3.Error:
            pass  # Une erreur disque ne permet aucune progression de curseur.
        self.failed = True

    def status(self):
        self._active()
        meta = self._meta()
        offset = self.connection.execute("SELECT offset FROM epochs WHERE id=?", (meta["epoch"],)).fetchone()[0]
        return {"simulation_only": True, "epoch": meta["epoch"], "state": meta["state"],
                "reason": meta["reason"], "stored_offset": offset,
                "next_offset": None if meta["state"] == "recovery_required" else offset,
                "receipts": self.connection.execute("SELECT count(*) FROM receipts").fetchone()[0],
                "messages": self.connection.execute("SELECT count(*) FROM messages").fetchone()[0],
                "pending": self.connection.execute("SELECT count(*) FROM messages WHERE status='pending'").fetchone()[0]}

    def resume(self, now):
        self._active()
        now = _time(now)
        try:
            with self._transaction():
                meta = self._meta()
                if now < meta["last_clock"]:
                    meta.update(state="recovery_required", reason="clock_reversed")
                elif now - meta["last_update"] >= 7*86400:
                    meta.update(state="recovery_required", reason="epoch_review")
                elif now - meta["last_receive"] >= 86400 and meta["state"] != "recovery_required":
                    meta.update(state="gap_possible", reason="retention_gap")
                meta["last_clock"] = max(now, meta["last_clock"])
                self._save_meta(meta)
                self.connection.execute("UPDATE messages SET status='expired' WHERE status='pending' AND date < ?", (now-120,))
            return self.status()
        except sqlite3.Error:
            self.failed = True
            raise StoreError("STORAGE_FAILED") from None

    def _project(self, update, now, meta, historical):
        if not isinstance(update, dict) or type(update.get("update_id")) is not int or not 0 <= update["update_id"] < 2**52:
            raise StoreError("BATCH_INVALID")
        message = update.get("message")
        if (set(update) != {"update_id", "message"}
                or not private_router.authenticate(message, self.config)):
            return update["update_id"], None, "rejected"
        if (not _positive(message.get("message_id")) or type(message.get("date")) is not int
                or not 0 <= message["date"] < 2**52 or not isinstance(message.get("text"), str)
                or not 0 < len(message["text"]) <= private_router.MAX_COMMAND_LENGTH):
            return update["update_id"], None, "rejected"
        if message["date"] > now + 30:
            raise StoreError("CLOCK_FUTURE")
        payload = {"message_id": message["message_id"], "date": message["date"],
                   "text": message["text"], "from": {"id": message["from"]["id"], "is_bot": False},
                   "chat": {"id": message["chat"]["id"], "type": "private"}}
        disposition = ("historical" if historical or message["date"] < meta["activated_at"]
                       else "expired" if now-message["date"] > 120 else "admitted")
        return update["update_id"], payload, disposition

    def receive_batch(self, updates, expected_offset, now):
        """Retourne uniquement après commit ; ne confirme jamais rien à distance."""
        return self._receive(updates, expected_offset, now, recovery=False)

    def recover_epoch(self, updates, now, *, approval):
        """Réconciliation manuelle de fixtures ; nouveau lot exclusivement historique."""
        if approval != "reviewed-simulation-recovery":
            raise StoreError("APPROVAL_REQUIRED")
        return self._receive(updates, None, now, recovery=True)

    def _receive(self, updates, expected_offset, now, recovery):
        self._active()
        now = _time(now)
        try:
            self.resume(now)
            self._check_files()
            self._quota()
            self.fault("before_begin")
            with self._transaction():
                meta = self._meta()
                if recovery:
                    if meta["state"] != "recovery_required" or not updates or now < meta["last_clock"]:
                        raise StoreError("RECOVERY_INVALID")
                    meta["epoch"] += 1
                    self.connection.execute("INSERT INTO epochs VALUES(?,0,'manual_review')", (meta["epoch"],))
                    meta.update(state="gap_possible", reason="manual_epoch", last_update=now)
                elif meta["state"] == "recovery_required":
                    raise StoreError("RECOVERY_REQUIRED")
                epoch = meta["epoch"]
                offset = self.connection.execute("SELECT offset FROM epochs WHERE id=?", (epoch,)).fetchone()[0]
                if not recovery and (type(expected_offset) is not int or expected_offset != offset):
                    raise StoreError("CURSOR_MISMATCH")
                if not isinstance(updates, list) or len(updates) > MAX_BATCH:
                    raise StoreError("BATCH_INVALID")
                rows = [self._project(update, now, meta, recovery) for update in updates]
                ids = [row[0] for row in rows]
                if ids != sorted(set(ids)):
                    raise StoreError("BATCH_ORDER")
                new_messages = 0
                new_receipts = 0
                for update_id, payload, disposition in rows:
                    digest = _digest(payload) if payload else None
                    prior = self.connection.execute("SELECT digest FROM receipts WHERE epoch=? AND update_id=?", (epoch, update_id)).fetchone()
                    if prior:
                        if prior[0] != digest:
                            raise StoreError("UPDATE_CONFLICT")
                        continue
                    if update_id < offset:
                        raise StoreError("UPDATE_REGRESSION")
                    message_id = payload["message_id"] if payload else None
                    if payload:
                        previous = self.connection.execute("SELECT digest FROM messages WHERE message_id=?", (message_id,)).fetchone()
                        if previous:
                            if previous[0] != digest:
                                raise StoreError("MESSAGE_CONFLICT")
                            disposition = "duplicate_message"
                        else:
                            status = "pending" if disposition == "admitted" else disposition
                            self.connection.execute("INSERT INTO messages VALUES(?,?,?,?,?)",
                                (message_id, digest, json.dumps(payload), payload["date"], status))
                            new_messages += 1
                    self.connection.execute("INSERT INTO receipts VALUES(?,?,?,?,?,?)",
                        (epoch, update_id, digest, message_id, disposition, now))
                    new_receipts += 1
                    self.fault("after_receipt")
                if ids:
                    self.connection.execute("UPDATE epochs SET offset=? WHERE id=?", (max(offset, max(ids)+1), epoch))
                if new_receipts:
                    meta["last_update"] = now
                meta.update(last_receive=now, last_clock=now)
                self._save_meta(meta)
                self._quota()
                self.fault("before_commit")
            self.fault("after_commit")
            return {**self.status(), "new_messages": new_messages, "new_receipts": new_receipts}
        except (StoreError, sqlite3.Error, OSError) as error:
            code = str(error) if isinstance(error, StoreError) else "STORAGE_FAILED"
            if code not in REASONS:
                code = "INTERNAL_FAILURE"
            self._suspend(code)
            raise StoreError(code) from None

    def windows_acl_readiness(self):
        """Lecture ACL de fixtures uniquement ; ne change aucun droit.

        Non requis pour faire tourner une simulation Windows, mais échec fermé
        du diagnostic de préparation C. Aucun mode réel n'est exposé par B1.
        """
        self._active()
        try:
            self._check_files()
            for path in (self.root, *self.root.iterdir()):
                credential_acl.verify(path)
            return {"simulation_only": True, "acl_ready": True}
        except Exception:
            return {"simulation_only": True, "acl_ready": False}

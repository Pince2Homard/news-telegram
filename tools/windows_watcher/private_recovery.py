"""C2 : fencing de fixtures, double journal explicite, aucune activation.

Le témoin doit rester indépendant des sauvegardes du magasin. Un rollback
coordonné des deux journaux est indétectable ici : aucune garantie TPM/distante.
"""
from contextlib import ExitStack
import hashlib
import json
import os
import sqlite3
from uuid import uuid4

from . import coordination, private_store as base, watcher


class FenceError(RuntimeError):
    pass


SCHEMA = "CREATE TABLE events (seq INTEGER PRIMARY KEY, payload TEXT NOT NULL, previous TEXT NOT NULL, digest TEXT NOT NULL)"
MAX_EVENTS = 1000  # À saturation : bloquer, jamais effacer l'historique.


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def store_digest(path):
    path = base._safe_path(path)
    if (not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > base.MAX_DATABASE_BYTES
            or any(path.with_name(path.name+suffix).exists() for suffix in ("-journal", "-wal", "-shm"))):
        raise FenceError("STORE_PROOF_MISSING")
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


class FixtureFence:
    """Cycle manuel, sans boucle, réservé aux fixtures explicitement injectées.

    Acquérir AVANT d'ouvrir le magasin ; le fermer AVANT seal. L'état interrupted
    n'est jamais effacé par __exit__. Deux commits distincts : désaccord = blocage.
    """

    def __init__(self, directory, witness, store, binding, now, *, create=False, fault=None):
        self.root, self.witness, self.store = map(base._fixture_directory, (directory, witness, store))
        paths = (self.root, self.witness, self.store.parent)
        if any(a == b or a in b.parents or b in a.parents for i,a in enumerate(paths) for b in paths[i+1:]):
            raise FenceError("LOCATION_INVALID")
        self.identity = {"fixtures_only": True, "format": 1, "local": os.path.normcase(str(self.root)),
            "witness": os.path.normcase(str(self.witness)), "store": os.path.normcase(str(self.store)),
            "binding": json.loads(_json(binding))}
        if (type(create) is not bool or not isinstance(binding,dict)
                or set(binding)!={"simulation_only","bot_id","owner_id","chat_id","generation"}
                or binding["simulation_only"] is not True
                or not all(base._positive(binding[key]) for key in ("bot_id","owner_id","chat_id"))
                or not coordination.uuid_text(binding["generation"])):
            raise FenceError("BINDING_INVALID")
        self.now = base._time(now)
        self.create = create
        self.fault = fault or (lambda stage: None)
        self.connections = []
        self.stack = ExitStack()
        self.active = False
        self.poisoned = False
        self.local_job = None

    def __enter__(self):
        try:
            if self.create:
                # Ni adoption de dossier existant, ni recréation d'une preuve perdue.
                for root in (self.root, self.witness):
                    root.mkdir(mode=0o700, exist_ok=False)
            for root in sorted((self.root, self.witness)):
                base._safe_path(root)
                if not root.is_dir():
                    raise FenceError("PROOF_MISSING")
                self.stack.enter_context(watcher.single_instance_lock(root/"fence.lock"))
                allowed = {"fence.lock", "fence.sqlite3", "fence.sqlite3-journal"}
                if any(p.name not in allowed or not p.is_file() or p.stat().st_nlink != 1
                       or base._safe_path(p) != p for p in root.iterdir()):
                    raise FenceError("PROOF_FILES_INVALID")
                path = root/"fence.sqlite3"
                if self.create:
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    os.close(descriptor)
                if not path.is_file() or path.stat().st_size > 4*1024*1024:
                    raise FenceError("PROOF_MISSING")
                connection = sqlite3.connect(path.as_uri()+"?mode=rw", uri=True, isolation_level=None, timeout=.1)
                self.connections.append(connection)
                if not self.create and connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                    raise FenceError("PROOF_INVALID")
                for pragma in ("journal_mode=DELETE", "synchronous=EXTRA", "trusted_schema=OFF", "temp_store=MEMORY", "busy_timeout=100"):
                    connection.execute("PRAGMA "+pragma)
                if connection.execute("PRAGMA synchronous").fetchone()[0] != 3:
                    raise FenceError("DURABILITY_INVALID")
                if self.create:
                    connection.execute(SCHEMA)
                    connection.execute("PRAGMA user_version=1")
            if self.create:
                self.seq, self.previous = 0, "0"*64
                self.state = {"identity": self.identity, "phase": "sealed", "session": None,
                    "job": None, "owner": None, "clock": self.now, "dirty": False,
                    "store_digest": store_digest(self.store)}
                self._append(self.state)
            self._read()
            return self
        except BaseException as error:
            self.__exit__(None,None,None)
            if isinstance(error, (FenceError, KeyboardInterrupt, SystemExit)):
                raise
            raise FenceError("PROOF_UNAVAILABLE") from None

    def __exit__(self, *args):
        stop_error=None
        if self.local_job is not None and not self.local_job.safe:
            try:
                self.local_job.stop()
            except BaseException as error:
                if not self.local_job.safe:
                    # private_job garde aussi le job/fence en quarantaine :
                    # ne fermer ni les connexions ni les deux verrous ici.
                    raise
                # Arrêt OS prouvé mais écriture de preuve échouée : les journaux
                # restent non sealed, donc toute nouvelle session sera refusée.
                stop_error=error
        self.active = False
        for connection in self.connections:
            connection.close()
        self.connections = []
        self.stack.close()
        if stop_error is not None:
            raise stop_error

    def _read(self):
        histories = []
        for connection in self.connections:
            if (connection.execute("PRAGMA user_version").fetchone()[0] != 1
                    or dict(connection.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")) != {"events": SCHEMA}
                    or connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok"):
                raise FenceError("PROOF_INVALID")
            rows = connection.execute("SELECT * FROM events ORDER BY seq LIMIT ?", (MAX_EVENTS+1,)).fetchall()
            previous = "0"*64
            if not rows or len(rows)>MAX_EVENTS:
                raise FenceError("PROOF_QUOTA")
            for number,(seq,payload,prior,digest) in enumerate(rows,1):
                if seq != number or prior != previous or digest != _hash(prior+payload):
                    raise FenceError("PROOF_INVALID")
                previous = digest
            histories.append(rows)
        if len(histories)!=2 or histories[0]!=histories[1]:
            raise FenceError("WITNESS_MISMATCH")
        self.seq,payload,_,self.previous = histories[0][-1]
        self.state = json.loads(payload)
        if (set(self.state) != {"identity","phase","session","job","owner","clock","store_digest","dirty"}
                or self.state["identity"] != self.identity
                or self.state["phase"] not in {"sealed","running","intent","stopping","stopped","retired"}
                or type(self.state["dirty"]) is not bool
                or not isinstance(self.state["store_digest"],str) or len(self.state["store_digest"])!=64):
            raise FenceError("PROOF_IDENTITY")
        base._time(self.state["clock"])
        if self.now < self.state["clock"]:
            raise FenceError("CLOCK_REVERSED")

    def _append(self, state):
        if self.poisoned or not self.connections or self.seq >= MAX_EVENTS:
            raise FenceError("PROOF_QUOTA_OR_CLOSED")
        payload = _json(state)
        digest = _hash(self.previous+payload)
        try:
            for index,connection in enumerate(self.connections):
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute("INSERT INTO events VALUES(?,?,?,?)", (self.seq+1,payload,self.previous,digest))
                    connection.execute("COMMIT")
                except BaseException:
                    if connection.in_transaction:connection.execute("ROLLBACK")
                    raise
                self.fault("after_proof_commit_"+str(index))
            self.seq += 1
            self.previous, self.state = digest, state
        except BaseException:
            self.poisoned = True
            raise

    def _transition(self, phase, **changes):
        self._append({**self.state, **changes, "phase": phase, "clock": self.now})

    def begin(self):
        if self.active or self.poisoned or self.state["phase"] != "sealed":
            raise FenceError("PREVIOUS_SESSION_UNPROVEN")
        if store_digest(self.store) != self.state["store_digest"]:
            raise FenceError("STORE_RESTORED_OR_CHANGED")
        self._transition("running", session=str(uuid4()), job=None, owner=None, dirty=False)
        self.active = True
        return self.state["session"]

    def assert_active(self, session):
        if (not self.active or self.poisoned or session != self.state["session"]
                or self.state["phase"] not in {"running","intent","stopping","stopped"}):
            raise FenceError("STALE_GENERATION")

    def touch_store(self):
        """Durable AVANT toute ouverture du magasin, dont la reprise écrit déjà."""
        self.assert_active(self.state["session"])
        if not self.state["dirty"]:
            self._transition(self.state["phase"], dirty=True)

    def intent(self, job, owner, session):
        self.assert_active(session)
        if self.state["phase"] != "running" or self.state["job"] is not None:
            raise FenceError("JOB_ALREADY_BOUND")
        self._transition("intent", job=job, owner=owner)

    def stopping(self, job):
        self.assert_active(job.session)
        if self.local_job is not job or self.state["job"] != job.name:
            raise FenceError("JOB_MISMATCH")
        self._transition("stopping")

    def stopped(self, job):
        self.assert_active(job.session)
        if self.local_job is not job or not job.safe or job.final_counts["active"] != 0:
            raise FenceError("STOP_UNPROVEN")
        self._transition("stopped")

    def seal(self):
        self.assert_active(self.state["session"])
        if self.state["phase"] not in {"running","stopped"}:
            raise FenceError("STOP_UNPROVEN")
        if self.local_job is not None and not self.local_job.safe:
            raise FenceError("STOP_UNPROVEN")
        # Le verrou du magasin doit être disponible : l'appelant l'a fermé.
        with watcher.single_instance_lock(self.store.parent/"fixture.lock"):
            self._transition("sealed", store_digest=store_digest(self.store), dirty=False)
        self.active = False

    def recover_observed(self, observation):
        # Import local pour que la partie journal reste portable, sans WinAPI.
        from .private_fenced_job import WindowsObservation
        if (self.active or self.poisoned or type(observation) is not WindowsObservation
                or observation.identity != self.identity or observation.session != self.state["session"]
                or observation.name != self.state["job"] or observation.owner != self.state["owner"]):
            raise FenceError("OBSERVATION_MISMATCH")
        observation.terminate_and_prove()
        # Une écriture interrompue sans checkpoint externe complet reste désarmée :
        # un ancien snapshot pourrait avoir perdu une intention déjà acceptée.
        with watcher.single_instance_lock(self.store.parent/"fixture.lock"):
            if self.state["dirty"]:
                self._transition("retired")
            else:
                if store_digest(self.store) != self.state["store_digest"]:
                    raise FenceError("STORE_RESTORED_OR_CHANGED")
                self._transition("sealed")


def accept_result(fence, session, result):
    fence.assert_active(session)
    if (fence.state["phase"] != "stopped" or not isinstance(result,dict)
            or result.get("generation") != session):
        raise FenceError("STALE_RESULT")
    return result

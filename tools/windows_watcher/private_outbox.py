"""B2 : consommation et livraison de fixtures, sans API réseau ni boucle de polling."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape, unescape
import json
import sqlite3

from . import coordination, private_router, private_store as base, supervision


VERSION = 2
EXTRA_SCHEMA = {
    "delivery_identity": "CREATE TABLE delivery_identity (id INTEGER PRIMARY KEY CHECK(id=1), generation TEXT NOT NULL, binding TEXT NOT NULL, cooldown_until REAL NOT NULL CHECK(cooldown_until>=0))",
    "business_results": "CREATE TABLE business_results (message_id INTEGER PRIMARY KEY REFERENCES messages(message_id), response TEXT NOT NULL, digest TEXT NOT NULL, prepared_at REAL NOT NULL)",
    "outbox": "CREATE TABLE outbox (message_id INTEGER PRIMARY KEY REFERENCES business_results(message_id), destination INTEGER NOT NULL, generation TEXT NOT NULL, expires_at REAL NOT NULL, created_at REAL NOT NULL, frozen_digest TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('prepared','in_flight','confirmed','rejected','uncertain','expired','cancelled')))",
    "attempts": "CREATE TABLE attempts (message_id INTEGER PRIMARY KEY REFERENCES outbox(message_id), started_at REAL NOT NULL, status TEXT NOT NULL CHECK(status IN ('in_flight','confirmed','rejected','uncertain')), completed_at REAL, remote_message_id INTEGER UNIQUE, code TEXT NOT NULL CHECK(code IN ('intent','success','rejected','timeout','invalid','restart')))",
}
SCHEMA = {**base.SCHEMA, **EXTRA_SCHEMA}


@dataclass(frozen=True)
class SimulationPermission:
    bot_id: int
    owner_id: int
    chat_id: int
    generation: str
    enabled: bool = False
    simulation_only: bool = True


@dataclass(frozen=True)
class SimulatedRequest:
    message_id: int
    chat_id: int
    generation: str
    text: str
    expires_at: float
    parse_mode: str = "HTML"


@dataclass(frozen=True)
class SimulatedOutcome:
    kind: str = "success"
    delay: float = 0
    retry_after: int = 0


class ScriptedTransport:
    """Machine de fixtures fermée. Aucun callable externe, sleep, fichier ou socket."""

    def __init__(self, outcomes=()):
        self.outcomes = tuple(outcomes)
        for item in self.outcomes:
            if (type(item) is not SimulatedOutcome or item.kind not in {
                    "success", "reject", "timeout", "timeout_after_accept", "invalid",
                    "wrong_destination", "wrong_text", "bad_id"}
                    or type(item.retry_after) is not int or not 0 <= item.retry_after <= 86400):
                raise base.StoreError("SIMULATION_INVALID")
            base._time(item.delay)
        self.requests = []
        self.accepted = []

    def exchange(self, request):
        item = self.outcomes[len(self.requests)] if len(self.requests) < len(self.outcomes) else SimulatedOutcome()
        self.requests.append(request)
        accepted = item.kind in {"success", "timeout_after_accept", "wrong_destination", "wrong_text", "bad_id"}
        if accepted:
            self.accepted.append(request.message_id)
        reply = {"simulation": True, "elapsed": item.delay, "http_status": 200,
                 "ok": True, "result": {"message_id": 1000000 + request.message_id,
                    "chat": {"id": request.chat_id, "type": "private"}, "text": unescape(request.text)}}
        if item.kind in {"timeout", "timeout_after_accept"}:
            reply.update(http_status=None, ok=None, result=None)
        elif item.kind == "reject":
            reply.update(http_status=429 if item.retry_after else 403, ok=False,
                         result=None, retry_after=item.retry_after)
        elif item.kind == "invalid":
            reply["result"] = None
        elif item.kind == "wrong_destination":
            reply["result"]["chat"]["id"] = -1001234567890
        elif item.kind == "wrong_text":
            reply["result"]["text"] = "fixture mismatch"
        elif item.kind == "bad_id":
            reply["result"]["message_id"] = True
        return reply


class DeliveryStore(base.SimulationStore):
    """Schéma B2 distinct ; migration B1 explicite sous le même verrou.

    generation est un UUID de fixture injecté, épinglé dans la base. Cette
    permission de simulation ne constitue jamais une autorisation réseau.
    """

    def __init__(self, *args, generation, migrate=False, **kwargs):
        if not coordination.uuid_text(generation) or type(migrate) is not bool:
            raise base.StoreError("GENERATION_INVALID")
        self.generation = generation
        self.migrate = migrate
        super().__init__(*args, **kwargs)

    def _validate(self):
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 1:
            if not (self.create or self.migrate):
                raise base.StoreError("MIGRATION_REQUIRED")
            # Valider toute la source AVANT un DDL ; jamais migrer une base anormale.
            super()._validate()
            self.fault("before_migration")
            with self._transaction():
                for sql in EXTRA_SCHEMA.values():
                    self.connection.execute(sql)
                    self.fault("during_migration")
                self.connection.execute("INSERT INTO delivery_identity VALUES(1,?,?,0)",
                                        (self.generation, json.dumps(self.binding, sort_keys=True)))
                self.connection.execute(f"PRAGMA user_version={VERSION}")
                self._quota()
                self.fault("before_migration_commit")
            self.fault("after_migration_commit")
        super()._validate(schema=SCHEMA, version=VERSION)
        self._validate_delivery()

    def _identity(self):
        row = self.connection.execute("SELECT * FROM delivery_identity WHERE id=1").fetchone()
        if (row is None or row["generation"] != self.generation
                or json.loads(row["binding"]) != self.binding):
            raise base.StoreError("GENERATION_INVALID")
        base._time(row["cooldown_until"])
        return row

    def _frozen(self, row, response_digest):
        return base._digest([row["message_id"], row["destination"], row["generation"],
                             row["expires_at"], row["created_at"], response_digest])

    def _validate_response(self, response):
        if (not isinstance(response, dict) or set(response) != {
                "simulation", "sent", "authorized", "code", "command", "text", "parse_mode"}
                or response["simulation"] is not True or response["sent"] is not False
                or response["authorized"] is not True or response["parse_mode"] != "HTML"
                or response["code"] not in {"ok", "invalid_message", "unsupported_command"}
                or response["command"] not in {None, "/start", "/help", "/status", "/pr", "/ci", "/rapports"}
                or not isinstance(response["text"], str)
                or not 0 < len(response["text"]) <= private_router.MAX_RESPONSE_LENGTH
                or escape(unescape(response["text"]), quote=True) != response["text"]):
            raise base.StoreError("RESPONSE_INVALID")

    def _validate_delivery(self):
        self._identity()
        for row in self.connection.execute("""SELECT b.*,r.response,r.digest,r.prepared_at,m.date,m.status AS reservation
                FROM outbox b JOIN business_results r USING(message_id) JOIN messages m USING(message_id)"""):
            response = json.loads(row["response"])
            self._validate_response(response)
            if (base._digest(response) != row["digest"] or self._frozen(row, row["digest"]) != row["frozen_digest"]
                    or type(row["destination"]) is not int or row["destination"] != self.binding["chat_id"]
                    or row["generation"] != self.generation or row["expires_at"] != row["date"]+120
                    or row["created_at"] != row["prepared_at"] or row["reservation"] == "historical"):
                raise base.StoreError("OUTBOX_INVALID")
            base._time(row["created_at"])
            if row["created_at"] > row["expires_at"] or row["created_at"] > self._meta()["last_clock"]:
                raise base.StoreError("OUTBOX_INVALID")
        if self.connection.execute("""SELECT 1 FROM business_results r LEFT JOIN outbox b USING(message_id)
                WHERE b.message_id IS NULL LIMIT 1""").fetchone():
            raise base.StoreError("OUTBOX_INVALID")
        if self.connection.execute("""SELECT 1 FROM outbox b LEFT JOIN attempts a USING(message_id) WHERE
                (b.status IN ('in_flight','confirmed','rejected','uncertain') AND (a.message_id IS NULL OR a.status!=b.status))
                OR (b.status IN ('prepared','expired','cancelled') AND a.message_id IS NOT NULL) LIMIT 1""").fetchone():
            raise base.StoreError("ATTEMPT_INVALID")
        prior = None
        starts = []
        for row in self.connection.execute("SELECT a.*,b.expires_at,b.created_at FROM attempts a JOIN outbox b USING(message_id) ORDER BY started_at"):
            started = base._time(row["started_at"])
            if not row["created_at"] <= started <= row["expires_at"] or (prior is not None and started-prior < 2):
                raise base.StoreError("ATTEMPT_INVALID")
            starts = [value for value in starts if value > started-60]
            if len(starts) >= 10:
                raise base.StoreError("ATTEMPT_INVALID")
            starts.append(started)
            prior = started
            if row["status"] == "in_flight":
                if row["completed_at"] is not None or row["code"] != "intent" or row["remote_message_id"] is not None:
                    raise base.StoreError("ATTEMPT_INVALID")
            else:
                if (row["completed_at"] is None or not started <= base._time(row["completed_at"]) <= self._meta()["last_clock"]
                        or row["code"] not in {"confirmed": {"success"}, "rejected": {"rejected"},
                                               "uncertain": {"timeout", "invalid", "restart"}}[row["status"]]):
                    raise base.StoreError("ATTEMPT_INVALID")
                if (row["status"] == "confirmed") != base._positive(row["remote_message_id"]):
                    raise base.StoreError("ATTEMPT_INVALID")

    def _on_open(self):
        # Le verrou garantit qu'aucune tentative d'un autre processus local ne vit.
        with self._transaction():
            self.connection.execute("UPDATE attempts SET status='uncertain',completed_at=MAX(?,started_at),code='restart' WHERE status='in_flight'", (self.now,))
            self.connection.execute("UPDATE outbox SET status='uncertain' WHERE status='in_flight'")
            self._expire(self.now)

    def _expire(self, now):
        self.connection.execute("UPDATE outbox SET status='expired' WHERE status='prepared' AND expires_at < ?", (now,))

    @contextmanager
    def _operation(self):
        self._active()
        try:
            yield
        except Exception as error:
            self._suspend("STORAGE_FAILED" if isinstance(error, (sqlite3.Error, OSError)) else "INTERNAL_FAILURE")
            raise base.StoreError("STORAGE_FAILED" if isinstance(error, (sqlite3.Error, OSError)) else "DELIVERY_REFUSED") from None

    def _guard(self, now, permission=None):
        now = base._time(now)
        self._check_files()
        self._validate()
        config = supervision.validate_config(self.config)
        if config["owner_user_id"] != self.binding["owner_id"] or config["arbitration_private_chat_id"] != self.binding["chat_id"]:
            raise base.StoreError("IDENTITY_INVALID")
        state = self.resume(now)
        if state["state"] == "recovery_required":
            raise base.StoreError("RECOVERY_REQUIRED")
        if permission is not None:
            expected = SimulationPermission(self.binding["bot_id"], self.binding["owner_id"],
                                             self.binding["chat_id"], self.generation, True, True)
            if type(permission) is not SimulationPermission or permission != expected:
                raise base.StoreError("PERMISSION_INVALID")
            if any(type(value) is not int for value in (permission.bot_id, permission.owner_id, permission.chat_id)):
                raise base.StoreError("PERMISSION_INVALID")
            if permission.enabled is not True or permission.simulation_only is not True:
                raise base.StoreError("PERMISSION_INVALID")
        with self._transaction():
            self._expire(now)
        return now

    def prepare(self, message_id, snapshot, now):
        """Calcul pur et résultat/outbox atomiques ; snapshot exclusivement injecté."""
        with self._operation():
            if not base._positive(message_id):
                raise base.StoreError("MESSAGE_INVALID")
            now = self._guard(now)
            self.fault("before_prepare")
            with self._transaction():
                prior = self.connection.execute("SELECT response FROM business_results WHERE message_id=?", (message_id,)).fetchone()
                if prior:
                    return json.loads(prior[0])
                message = self.connection.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
                if message is None or message["status"] != "pending" or now > message["date"]+120:
                    return None
                payload = json.loads(message["payload"])
                if not private_router.authenticate(payload, self.config):
                    raise base.StoreError("IDENTITY_INVALID")
                response = private_router.route_message(payload, self.config, snapshot,
                                                        datetime.fromtimestamp(now, timezone.utc))
                self._validate_response(response)
                digest = base._digest(response)
                self.connection.execute("INSERT INTO business_results VALUES(?,?,?,?)",
                                        (message_id, json.dumps(response), digest, now))
                self.fault("after_business_result")
                row = {"message_id": message_id, "destination": self.binding["chat_id"],
                       "generation": self.generation, "expires_at": float(message["date"]+120), "created_at": now}
                self.connection.execute("INSERT INTO outbox VALUES(?,?,?,?,?,?,?)",
                    (message_id, row["destination"], self.generation, row["expires_at"], now,
                     self._frozen(row, digest), "prepared"))
                self._quota()
                self.fault("before_prepare_commit")
            self.fault("after_prepare_commit")
            return response

    def inspect_outbox(self, message_id):
        self._active()
        if not base._positive(message_id):
            raise base.StoreError("MESSAGE_INVALID")
        self._check_files()
        self._validate()
        row = self.connection.execute("SELECT * FROM outbox WHERE message_id=?", (message_id,)).fetchone()
        return dict(row) if row else None

    def cancel(self, message_id, now):
        with self._operation():
            if not base._positive(message_id):
                raise base.StoreError("MESSAGE_INVALID")
            self._guard(now)
            with self._transaction():
                self.connection.execute("UPDATE outbox SET status='cancelled' WHERE message_id=? AND status='prepared'", (message_id,))

    def deliver(self, message_id, transport, now, *, permission):
        """Une tentative simulée maximum ; aucune méthode Telegram ni sleep."""
        with self._operation():
            if type(transport) is not ScriptedTransport or permission is None or not base._positive(message_id):
                raise base.StoreError("SIMULATION_INVALID")
            now = self._guard(now, permission)
            self.fault("before_intent")
            with self._transaction():
                row = self.connection.execute("""SELECT b.*,r.response FROM outbox b
                    JOIN business_results r USING(message_id) WHERE b.message_id=?""", (message_id,)).fetchone()
                if row is None:
                    return "absent"
                if row["status"] != "prepared":
                    return row["status"]
                starts = self.connection.execute("SELECT MAX(started_at),COUNT(*) FROM attempts WHERE started_at > ?", (now-60,)).fetchone()
                if ((starts[0] is not None and now-starts[0] < 2) or starts[1] >= 10
                        or now < self._identity()["cooldown_until"]):
                    return "rate_limited"
                response = json.loads(row["response"])
                request = SimulatedRequest(message_id, row["destination"], row["generation"],
                                           response["text"], row["expires_at"])
                self.connection.execute("INSERT INTO attempts VALUES(?,?,'in_flight',NULL,NULL,'intent')", (message_id, now))
                self.connection.execute("UPDATE outbox SET status='in_flight' WHERE message_id=?", (message_id,))
                self._quota()
                self.fault("before_intent_commit")
            self.fault("after_intent_commit")
            # Appel à une classe de simulation fermée, sans callback externe.
            reply = ScriptedTransport.exchange(transport, request)
            elapsed = base._time(reply["elapsed"])
            completed = now + min(elapsed, 20)
            status, code, remote_id = "uncertain", "invalid", None
            if elapsed > 20 or reply["http_status"] is None:
                code = "timeout"
            elif reply["ok"] is False and reply["http_status"] in (403, 429):
                status, code = "rejected", "rejected"
            elif reply["ok"] is True and reply["http_status"] == 200:
                result = reply["result"]
                if (isinstance(result, dict) and base._positive(result.get("message_id"))
                        and result.get("chat") == {"id": request.chat_id, "type": "private"}
                        and result.get("text") == unescape(request.text)):
                    status, code, remote_id = "confirmed", "success", result["message_id"]
            with self._transaction():
                self.connection.execute("UPDATE attempts SET status=?,completed_at=?,remote_message_id=?,code=? WHERE message_id=?",
                                        (status, completed, remote_id, code, message_id))
                self.connection.execute("UPDATE outbox SET status=? WHERE message_id=?", (status, message_id))
                meta = self._meta()
                meta["last_clock"] = completed
                self._save_meta(meta)
                if status == "rejected" and reply.get("retry_after", 0):
                    self.connection.execute("UPDATE delivery_identity SET cooldown_until=? WHERE id=1", (completed+reply["retry_after"],))
                self._quota()
                self.fault("before_result_commit")
            self.fault("after_result_commit")
            return status

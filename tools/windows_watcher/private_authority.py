"""C5 : autorité/exécuteur de fixtures sans socket, ticket ou credential réel.

SQLite sérialise le modèle, pas un service externe. Restaurer cette base peut
réhabiliter ses permissions : limite volontairement exposée par un test négatif.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from html import unescape
import json
import os
import re
import sqlite3

from . import coordination, private_outbox as outbox, private_store as base


class AuthorityError(RuntimeError):
    classification = "permission_refused"


class AuthorityUnavailable(AuthorityError):
    classification = "unavailable_unknown"


@dataclass(frozen=True)
class FixtureOperation:
    op_id: str
    host_id: str
    generation: int
    revision: int
    checkpoint: str
    action: str
    message_id: int
    text: str = "fixture"

    def __post_init__(self):
        if (not coordination.uuid_text(self.op_id) or not coordination.uuid_text(self.host_id)
                or type(self.generation) is not int or self.generation<1
                or type(self.revision) is not int or self.revision<0
                or not checkpoint_valid(self.checkpoint) or self.action not in {"read","response"}
                or not base._positive(self.message_id) or not isinstance(self.text,str)
                or not self.text.startswith("fixture") or len(self.text)>256):
            raise AuthorityError("OPERATION_INVALID")


def checkpoint_valid(value):
    return isinstance(value,str) and re.fullmatch(r"[0-9a-f]{64}",value) is not None


SCHEMA = {
    "authority": "CREATE TABLE authority (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)",
    "operations": "CREATE TABLE operations (op_id TEXT PRIMARY KEY, digest TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN ('read','response')), message_id INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('consumed','dispatching','confirmed','uncertain')), result TEXT NOT NULL, UNIQUE(action,message_id))",
}


class FixtureAuthority:
    """Un appel explicite = une opération synthétique ; aucun orchestrateur.

    host_id est un binding logique, pas une identité physique authentifiée.
    transport doit être exactement la machine de fixtures B2 existante.
    fault est uniquement un point de panne du banc.
    """

    def __init__(self, directory, *, trial_id, host_id, now, create=False, fault=None, transport=None, clock=None):
        if (not coordination.uuid_text(trial_id) or not coordination.uuid_text(host_id)
                or type(create) is not bool):
            raise AuthorityError("IDENTITY_INVALID")
        self.root=base._fixture_directory(directory)
        self.trial_id,self.host_id,self.now,self.create=trial_id,host_id,base._time(now),create
        self.transport=outbox.ScriptedTransport() if transport is None else transport
        if type(self.transport) is not outbox.ScriptedTransport:raise AuthorityError("FIXTURE_TRANSPORT_REQUIRED")
        self.fault=fault or (lambda stage:None)
        self.clock=clock
        self.db=None

    def __enter__(self):
        try:
            if self.create:self.root.mkdir(mode=0o700,exist_ok=False)
            if not self.root.is_dir():raise AuthorityUnavailable("AUTHORITY_MISSING")
            path=self.root/"authority.sqlite3"
            for child in self.root.iterdir():
                base._safe_path(child)
                if (child.name not in {"authority.sqlite3","authority.sqlite3-journal"}
                        or not child.is_file() or child.stat().st_nlink!=1 or child.stat().st_size>1024*1024):
                    raise AuthorityUnavailable("AUTHORITY_FILES_INVALID")
            if self.create:
                descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                os.close(descriptor)
            if not path.is_file():raise AuthorityUnavailable("AUTHORITY_MISSING")
            self.db=sqlite3.connect(path.as_uri()+"?mode=rw",uri=True,isolation_level=None,timeout=2)
            if not self.create and self.db.execute("PRAGMA journal_mode").fetchone()[0]!="delete":
                raise AuthorityUnavailable("AUTHORITY_INVALID")
            for pragma in ("journal_mode=DELETE","synchronous=EXTRA","trusted_schema=OFF","temp_store=MEMORY","busy_timeout=2000"):
                self.db.execute("PRAGMA "+pragma)
            if self.db.execute("PRAGMA synchronous").fetchone()[0]!=3:
                raise AuthorityUnavailable("DURABILITY_INVALID")
            if self.create:
                with self._transaction():
                    for sql in SCHEMA.values():self.db.execute(sql)
                    self.db.execute("PRAGMA user_version=1")
                    data=dict(fixtures_only=True,trial_id=self.trial_id,path=str(self.root),host_id=self.host_id,
                        generation=1,revision=0,checkpoint="0"*64,state="active",pending=None,
                        reads=0,responses=0,clock=self.now,deadline=self.now+300)
                    self.db.execute("INSERT INTO authority VALUES(1,?)",(json.dumps(data),))
            with self._transaction():self._validate()
            return self
        except BaseException as error:
            self.__exit__(None,None,None)
            if isinstance(error,(AuthorityError,KeyboardInterrupt,SystemExit)):raise
            raise AuthorityUnavailable("AUTHORITY_UNAVAILABLE") from None

    def __exit__(self,*args):
        if self.db is not None:self.db.close();self.db=None

    @contextmanager
    def _transaction(self):
        if self.db is None:raise AuthorityUnavailable("AUTHORITY_CLOSED")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            yield
            self.db.execute("COMMIT")
        except BaseException as error:
            if self.db.in_transaction:self.db.execute("ROLLBACK")
            if isinstance(error,sqlite3.Error):
                raise AuthorityUnavailable("AUTHORITY_STORAGE_UNKNOWN") from None
            raise

    def _data(self):
        try:return json.loads(self.db.execute("SELECT data FROM authority WHERE id=1").fetchone()[0])
        except (ValueError,TypeError):raise AuthorityUnavailable("AUTHORITY_INVALID") from None

    def _save(self,data):
        self.db.execute("UPDATE authority SET data=? WHERE id=1",(json.dumps(data,allow_nan=False),))

    def _validate(self):
        if (self.db.execute("PRAGMA user_version").fetchone()[0]!=1
                or dict(self.db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))!=SCHEMA
                or self.db.execute("PRAGMA integrity_check").fetchone()[0]!="ok"):
            raise AuthorityUnavailable("AUTHORITY_INVALID")
        data=self._data()
        fields={"fixtures_only","trial_id","path","host_id","generation","revision","checkpoint",
                "state","pending","reads","responses","clock","deadline"}
        if (set(data)!=fields or data["fixtures_only"] is not True or data["trial_id"]!=self.trial_id
                or data["path"]!=str(self.root) or not coordination.uuid_text(data["host_id"])
                or data["state"] not in {"active","revoked","expired","recovery_required"}
                or not checkpoint_valid(data["checkpoint"])
                or any(type(data[k]) is not int or not 0<=data[k]<=maximum for k,maximum in
                       (("reads",20),("responses",6),("revision",1000),("generation",1000)))
                or data["generation"]<1):
            raise AuthorityUnavailable("AUTHORITY_IDENTITY")
        base._time(data["clock"]);base._time(data["deadline"])
        counts=dict(self.db.execute("SELECT action,count(*) FROM operations GROUP BY action"))
        if counts.get("read",0)!=data["reads"] or counts.get("response",0)!=data["responses"]:
            raise AuthorityUnavailable("AUTHORITY_COUNTS_INVALID")
        if data["pending"] is not None and not self.db.execute("SELECT 1 FROM operations WHERE op_id=?",(data["pending"],)).fetchone():
            raise AuthorityUnavailable("AUTHORITY_PENDING_INVALID")
        return data

    def _live(self,data,now):
        now=base._time(now)
        if now<data["clock"] and data["state"]=="active":
            data["state"]="recovery_required"
        elif now>=data["deadline"] and data["state"]=="active":data["state"]="expired"
        data["clock"]=max(now,data["clock"])
        self._save(data)
        return data["state"]=="active"

    def _now(self,logical_time):
        # Temps d'autorité injecté par le banc, jamais une assertion du PC sur
        # le futur protocole. clock est relue aux frontières après une pause.
        return base._time(logical_time if self.clock is None else self.clock())

    def status(self):
        with self._transaction():return dict(self._validate())

    def execute(self,operation,now):
        if type(operation) is not FixtureOperation:raise AuthorityError("FIXTURE_OPERATION_REQUIRED")
        now=self._now(now)
        digest=base._digest(asdict(operation))
        denied=None
        with self._transaction():
            data=self._validate()
            if not self._live(data,now):denied="TRIAL_NOT_ACTIVE"
            elif self.db.execute("SELECT 1 FROM operations WHERE op_id=? OR (action=? AND message_id=?)",
                    (operation.op_id,operation.action,operation.message_id)).fetchone():denied="OPERATION_ALREADY_CONSUMED"
            elif operation.host_id!=data["host_id"] or operation.generation!=data["generation"]:denied="OWNER_OR_GENERATION"
            elif operation.revision!=data["revision"] or operation.checkpoint!=data["checkpoint"]:denied="CHECKPOINT_STALE"
            elif data["pending"] is not None:denied="OPERATION_PENDING"
            else:
                key,limit=("reads",20) if operation.action=="read" else ("responses",6)
                if data[key]>=limit:denied="QUOTA_EXCEEDED"
                else:
                    self.db.execute("INSERT INTO operations VALUES(?,?,?,?, 'consumed',?)",
                        (operation.op_id,digest,operation.action,operation.message_id,json.dumps({"code":"not_dispatched"})))
                    data[key]+=1;data["revision"]+=1;data["pending"]=operation.op_id
                    self._save(data)
                    self.fault("before_consume_commit")
        if denied:raise AuthorityError(denied)
        self.fault("after_consume_commit")
        self.fault("before_dispatch")
        with self._transaction():
            data=self._validate()
            live=self._live(data,self._now(now))
            # Une révocation ou un acquittement concurrent avant la frontière
            # empêche l'effet, même si la consommation avait déjà réussi.
            dispatch=(live and data["pending"]==operation.op_id and data["host_id"]==operation.host_id
                      and data["generation"]==operation.generation)
            if dispatch:
                self.db.execute("UPDATE operations SET state='dispatching',result=? WHERE op_id=?",
                    (json.dumps({"code":"effect_possible"}),operation.op_id))
        if not dispatch:return self.inspect(operation.op_id)
        self.fault("after_dispatch_commit")
        # Aucun ticket n'est remis au PC ; la seule capacité est ce modèle fermé.
        request=outbox.SimulatedRequest(operation.message_id,12345,"11111111-1111-4111-8111-111111111111",
                                       operation.text,now+20)
        reply=outbox.ScriptedTransport.exchange(self.transport,request)
        self.fault("after_effect")
        state="uncertain"
        if (reply.get("ok") is True and reply.get("http_status")==200
                and reply.get("elapsed",21)<=20 and isinstance(reply.get("result"),dict)
                and reply["result"].get("chat")=={"id":12345,"type":"private"}
                and reply["result"].get("text")==unescape(operation.text)
                and base._positive(reply["result"].get("message_id"))):state="confirmed"
        completed=max(self._now(now),now+min(base._time(reply.get("elapsed",21)),20))
        with self._transaction():
            data=self._validate()
            if not self._live(data,completed):state="uncertain"
            self.db.execute("UPDATE operations SET state=?,result=? WHERE op_id=?",
                (state,json.dumps({"code":"fixture_success" if state=="confirmed" else "effect_unknown"}),operation.op_id))
            self.fault("before_result_commit")
        return self.inspect(operation.op_id)

    def inspect(self,op_id):
        if not coordination.uuid_text(op_id):raise AuthorityError("OPERATION_INVALID")
        with self._transaction():
            data=self._validate()
            row=self.db.execute("SELECT state,result,digest FROM operations WHERE op_id=?",(op_id,)).fetchone()
            if row is None:raise AuthorityError("OPERATION_ABSENT")
            classification={"consumed":"consumed_without_operation","dispatching":"uncertain",
                            "confirmed":"confirmed","uncertain":"uncertain"}[row[0]]
            terminal=(row[0] in {"confirmed","uncertain"} or
                      (row[0]=="consumed" and (data["pending"]!=op_id or data["state"]!="active")))
            return dict(op_id=op_id,classification=classification,state=row[0],terminal=terminal,
                        result=json.loads(row[1]),digest=row[2])

    def acknowledge(self,op_id,revision,checkpoint_new,now):
        if not checkpoint_valid(checkpoint_new) or type(revision) is not int:raise AuthorityError("CHECKPOINT_INVALID")
        now=self._now(now)
        with self._transaction():
            data=self._validate()
            row=self.db.execute("SELECT state FROM operations WHERE op_id=?",(op_id,)).fetchone()
            if (data["pending"]!=op_id or data["revision"]!=revision or row is None
                    or row[0] not in {"consumed","confirmed"} or now<data["clock"]):
                raise AuthorityError("CHECKPOINT_REFUSED")
            data.update(pending=None,checkpoint=checkpoint_new,revision=data["revision"]+1,clock=base._time(now))
            self._save(data)

    def rotate(self,host_id,now):
        if not coordination.uuid_text(host_id):raise AuthorityError("HOST_INVALID")
        now=self._now(now)
        denied=False
        with self._transaction():
            data=self._validate()
            if not self._live(data,now) or data["pending"] is not None:denied=True
            else:
                data.update(host_id=host_id,generation=data["generation"]+1,revision=data["revision"]+1)
                self._save(data)
        if denied:raise AuthorityError("ROTATION_REFUSED")

    def revoke(self,now):
        now=self._now(now)
        with self._transaction():
            data=self._validate()
            data.update(state="revoked",revision=data["revision"]+1,clock=max(base._time(now),data["clock"]))
            self._save(data)

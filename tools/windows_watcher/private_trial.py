"""C3 : autorisation et quotas de fixtures, aucun mode Telegram activable."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import os
import sqlite3
import time

from . import coordination, private_store as base, watcher


class TrialError(RuntimeError):
    pass


@dataclass(frozen=True)
class FixturePlan:
    duration_seconds: int = 300
    read_limit: int = 20
    response_limit: int = 6
    fixture_enabled: bool = False
    telegram_enabled: bool = False

    def __post_init__(self):
        if (type(self.fixture_enabled) is not bool or self.telegram_enabled is not False
                or any(type(v) is not int or not 1<=v<=limit for v,limit in (
                    (self.duration_seconds,300),(self.read_limit,20),(self.response_limit,6)))):
            raise TrialError("PLAN_INVALID")


@dataclass(frozen=True)
class ManualFixtureEnrollment:
    bot_id: int
    owner_id: int
    chat_id: int
    deployment_generation: str
    host_id: str
    trial_id: str

    def __post_init__(self):
        if (not all(base._positive(v) for v in (self.bot_id,self.owner_id,self.chat_id))
                or not all(coordination.uuid_text(v) for v in
                           (self.deployment_generation,self.host_id,self.trial_id))):
            raise TrialError("ENROLLMENT_INVALID")


SCHEMA="CREATE TABLE trial (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)"
STATES={"disabled","active","closed","revoked","expired","exhausted","recovery_required"}


class FixtureTrial:
    """Session manuelle bornée. Réouvrir un essai actif après crash le désarme.

    Les compteurs sont consommés AVANT tentative et ne sont jamais remboursés.
    Ce fichier local ne constitue pas une autorité anti-rollback.
    """

    def __init__(self, directory, enrollment, plan=FixturePlan(), *, create=False,
                 wall=time.time, monotonic=time.monotonic):
        if type(enrollment) is not ManualFixtureEnrollment or type(plan) is not FixturePlan or type(create) is not bool:
            raise TrialError("FIXTURE_REQUIRED")
        self.root=base._fixture_directory(directory)
        self.enrollment,self.plan,self.create=enrollment,plan,create
        self.wall,self.monotonic=wall,monotonic
        self.db,self.lock,self.job=None,None,None
        self.started_mono=self.last_mono=None
        self.native_deadline=None
        self.failed=False

    def __enter__(self):
        try:
            if self.create:self.root.mkdir(mode=0o700,exist_ok=False)
            if not self.root.is_dir():raise TrialError("TRIAL_MISSING")
            self.lock=watcher.single_instance_lock(self.root/"trial.lock")
            self.lock.__enter__()
            self._files()
            path=self.root/"trial.sqlite3"
            if not self.create and not path.is_file():raise TrialError("TRIAL_MISSING")
            if self.create:
                descriptor=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                os.close(descriptor)
            self.db=sqlite3.connect(path.as_uri()+"?mode=rw",
                                    uri=True,isolation_level=None,timeout=.1)
            if not self.create and self.db.execute("PRAGMA journal_mode").fetchone()[0]!="delete":
                raise TrialError("TRIAL_INVALID")
            for pragma in ("journal_mode=DELETE","synchronous=EXTRA","trusted_schema=OFF","temp_store=MEMORY","busy_timeout=100"):
                self.db.execute("PRAGMA "+pragma)
            if self.db.execute("PRAGMA synchronous").fetchone()[0]!=3:raise TrialError("DURABILITY_INVALID")
            if self.create:
                with self._transaction():
                    self.db.execute(SCHEMA)
                    self.db.execute("PRAGMA user_version=1")
                    self.db.execute("INSERT INTO trial VALUES(1,?)",(json.dumps({"enrollment":asdict(self.enrollment),
                        "plan":asdict(self.plan),"state":"disabled","reads":0,"responses":0,
                        "started":None,"last_wall":None}),))
            self._read()
            if self.data["state"]=="active":
                self._save(state="recovery_required")
                raise TrialError("REBOOT_OR_CRASH_REQUIRES_REVIEW")
            return self
        except BaseException as error:
            self.failed=True
            self.__exit__(type(error),error,None)
            if isinstance(error,(TrialError,KeyboardInterrupt,SystemExit)):raise
            raise TrialError("TRIAL_UNAVAILABLE") from None

    def __exit__(self, kind, error, traceback):
        stop_error=None
        if self.job is not None and not self.job.safe:
            try:self.job.stop()
            except BaseException as caught:
                if not self.job.safe:raise  # Ne libérer aucun verrou.
                stop_error=caught
                kind=type(caught)
        try:
            if self.db is not None and not self.failed and getattr(self,"data",{}).get("state")=="active":
                self._save(state="closed" if kind is None else "recovery_required")
        finally:
            if self.db is not None:self.db.close();self.db=None
            if self.lock is not None:self.lock.__exit__(None,None,None);self.lock=None
        if stop_error is not None:raise stop_error

    def _files(self):
        base._safe_path(self.root)
        for path in self.root.iterdir():
            base._safe_path(path)
            if (path.name not in {"trial.lock","trial.sqlite3","trial.sqlite3-journal"}
                    or not path.is_file() or path.stat().st_nlink!=1 or path.stat().st_size>65536):
                raise TrialError("TRIAL_FILES_INVALID")

    @contextmanager
    def _transaction(self):
        try:
            self.db.execute("BEGIN IMMEDIATE")
            yield
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:self.db.execute("ROLLBACK")
            self.failed=True
            raise

    def _read(self):
        if (self.db.execute("PRAGMA user_version").fetchone()[0]!=1
                or dict(self.db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))!={"trial":SCHEMA}
                or self.db.execute("PRAGMA integrity_check").fetchone()[0]!="ok"):
            raise TrialError("TRIAL_INVALID")
        self.data=json.loads(self.db.execute("SELECT data FROM trial WHERE id=1").fetchone()[0])
        if (set(self.data)!={"enrollment","plan","state","reads","responses","started","last_wall"}
                or self.data["enrollment"]!=asdict(self.enrollment) or self.data["plan"]!=asdict(self.plan)
                or self.data["state"] not in STATES
                or any(type(self.data[key]) is not int or not 0<=self.data[key]<=limit
                       for key,limit in (("reads",self.plan.read_limit),("responses",self.plan.response_limit)))):
            raise TrialError("TRIAL_IDENTITY_INVALID")
        if self.data["started"] is not None:
            base._time(self.data["started"]);base._time(self.data["last_wall"])
            if self.data["last_wall"]<self.data["started"]:raise TrialError("CLOCK_INVALID")
        elif (self.data["state"] not in {"disabled","revoked"} or self.data["last_wall"] is not None
              or self.data["reads"] or self.data["responses"]):
            raise TrialError("TRIAL_INVALID")

    def _save(self, **changes):
        candidate={**self.data,**changes}
        with self._transaction():
            self.db.execute("UPDATE trial SET data=? WHERE id=1",(json.dumps(candidate,allow_nan=False),))
        self.data=candidate

    def authorize_fixture(self, *, approval, enrollment):
        if (self.db is None or self.failed or not self.plan.fixture_enabled
                or self.data["state"]!="disabled" or approval!="reviewed-synthetic-trial"
                or type(enrollment) is not ManualFixtureEnrollment or enrollment!=self.enrollment):
            raise TrialError("EXPLICIT_FIXTURE_APPROVAL_REQUIRED")
        wall,mono=base._time(self.wall()),base._time(self.monotonic())
        self.native_deadline=time.monotonic()+self.plan.duration_seconds
        self._save(state="active",started=wall,last_wall=wall)
        self.started_mono=self.last_mono=mono

    def remaining(self):
        if self.db is None or self.failed or self.data["state"]!="active" or self.started_mono is None:
            raise TrialError("TRIAL_NOT_ACTIVE")
        wall,mono=base._time(self.wall()),base._time(self.monotonic())
        if wall<self.data["last_wall"] or mono<self.last_mono:
            self._save(state="recovery_required")
            raise TrialError("CLOCK_REVERSED")
        remaining=min(self.plan.duration_seconds-(wall-self.data["started"]),
                      self.plan.duration_seconds-(mono-self.started_mono),self.native_deadline-time.monotonic())
        if remaining<=0:
            self._save(state="expired")
            raise TrialError("TRIAL_EXPIRED")
        self._save(last_wall=wall)
        self.last_mono=mono
        return remaining

    def reserve(self, action):
        if action not in {"read","response"}:raise TrialError("ACTION_INVALID")
        remaining=self.remaining()
        key,limit=("reads",self.plan.read_limit) if action=="read" else ("responses",self.plan.response_limit)
        if self.data[key]>=limit:
            self._save(state="exhausted")
            raise TrialError("TRIAL_QUOTA")
        self._save(**{key:self.data[key]+1})
        return remaining

    def emergency_stop(self):
        if self.job is not None and not self.job.safe:self.job.stop()
        if self.db is None or self.failed:raise TrialError("TRIAL_UNAVAILABLE")
        self._save(state="revoked")

    def suspend(self):
        if self.job is not None and not self.job.safe:self.job.stop()
        if self.db is not None and not self.failed and self.data["state"]=="active":
            self._save(state="recovery_required")

    def status(self):
        return {key:self.data[key] for key in ("state","reads","responses")}

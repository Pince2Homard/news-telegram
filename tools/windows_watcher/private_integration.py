"""C2/F3 : raccords privés explicites, transport local seulement, sans boucle."""
import json
import time

from . import private_boundary, private_contracts as contracts, private_outbox as outbox
from . import private_qualification as qualification, private_recovery as recovery, private_router, private_store as base
from . import private_bot_adapter as api, private_credential as credential, private_stop, private_trial as trial


def _bound(fence, store):
    if type(fence) is not recovery.FixtureFence or type(store) not in (outbox.DeliveryStore, qualification.QualifiedStore):
        raise recovery.FenceError("FIXTURE_REQUIRED")
    private_boundary.shared(fence,store)
    fence.assert_active(fence.state["session"])
    if (not fence.state["dirty"] or store.path != fence.store
            or {**store.binding,"generation":store.generation} != fence.identity["binding"]):
        raise recovery.FenceError("STORE_NOT_BOUND")


def receive_fixture(fence, store, raw, expected_offset, now):
    _bound(fence,store)
    # Les reçus minimaux rejetés sont déjà projetés en rejet B1 : pas de payload
    # artificiel, pas de transformation en message propriétaire.
    updates=contracts.validate_updates(raw,store.config)
    return store.receive_batch(updates,expected_offset,now)


def prepare_fixture(fence, store, message_id, snapshot_loader, now):
    _bound(fence,store)
    if not base._positive(message_id):
        raise recovery.FenceError("MESSAGE_INVALID")
    # Authentifier et vérifier fraîcheur AVANT d'appeler le lecteur d'instantané.
    now=store._guard(now)
    row=store.connection.execute("SELECT payload,date,status FROM messages WHERE message_id=?",(message_id,)).fetchone()
    if row is None or row["status"]!="pending" or now>row["date"]+120:
        return None
    if not private_router.authenticate(json.loads(row["payload"]),store.config):
        raise recovery.FenceError("IDENTITY_INVALID")
    prior=store.connection.execute("SELECT response FROM business_results WHERE message_id=?",(message_id,)).fetchone()
    if prior:return json.loads(prior[0])
    return store.prepare(message_id,snapshot_loader(),now)


def prepare_trial_fixture(fence, store, message_id, snapshot_loader, now):
    """F3 : autoriser exactement deux textes, avant toute lecture de vue."""
    _bound(fence,store)
    if not base._positive(message_id):raise recovery.FenceError("MESSAGE_INVALID")
    now=store._guard(now)
    row=store.connection.execute("SELECT payload,date,status FROM messages WHERE message_id=?",(message_id,)).fetchone()
    if row is None or row["status"]!="pending" or now>row["date"]+120:return None
    message=json.loads(row["payload"])
    if not private_router.authenticate(message,store.config):raise recovery.FenceError("IDENTITY_INVALID")
    if message["text"] not in {"/help","/status"}:return None
    prior=store.connection.execute("SELECT response FROM business_results WHERE message_id=?",(message_id,)).fetchone()
    if prior:return store.prepare(message_id,None,now,trial_only=True)
    return store.prepare(message_id,snapshot_loader() if message["text"]=="/status" else None,now,trial_only=True)


class FixtureIntegration:
    """Raccord explicite de l'unique contrôleur ; pas de polling ni de collecte.

    L'appelant possède déjà fence (begin/touch_store), magasin et trial. Aucun
    chemin, credential ou transport n'est découvert. Un appel local à la fois.
    """

    def __init__(self,fence,store,authorization,adapter,stop_gate,*,clock=time.time):
        _bound(fence,store)
        if (type(adapter) is not api.LocalBotAdapter or type(authorization) is not trial.FixtureTrial
                or type(stop_gate) is not private_stop.NativeStopGate
                or adapter.enrollment!=authorization.enrollment
                or (adapter.enrollment.owner_id,adapter.enrollment.chat_id)!=(store.binding["owner_id"],store.binding["chat_id"])
                or (adapter.target.bot_id,adapter.target.generation)!=(store.binding["bot_id"],store.generation)
                or stop_gate.data["session"]!=fence.state["session"]):
            raise recovery.FenceError("PRIVATE_BINDING_INVALID")
        self.fence,self.store,self.authorization=fence,store,authorization
        self.boundary=private_boundary.shared(fence,store,authorization,stop_gate)
        if self.boundary is not None:
            private_boundary.check(self.boundary,store=store.root,fence=fence.root,
                                   witness=fence.witness,trial=authorization.root,
                                   control=stop_gate.path.parent)
        self.adapter,self.stop_gate,self.clock=adapter,stop_gate,clock
        self.target,self.enrollment=adapter.target,authorization.enrollment
        self.closed=False
        self.preflights=set()

    def _admit(self):
        if self.closed:raise private_stop.StopError("ADMISSION_CLOSED")
        boundary=private_boundary.shared(self.fence,self.store,self.authorization,self.stop_gate)
        if boundary is not self.boundary:raise private_boundary.BoundaryError("PRIVATE_BOUNDARY_MISMATCH")
        if boundary is not None:
            private_boundary.check(boundary,store=self.store.root,fence=self.fence.root,
                                   witness=self.fence.witness,trial=self.authorization.root,
                                   control=self.stop_gate.path.parent)
        self.stop_gate.check()
        _bound(self.fence,self.store)
        if (self.adapter.target!=self.target or self.adapter.enrollment!=self.enrollment
                or self.authorization.enrollment!=self.enrollment
                or self.stop_gate.data["session"]!=self.fence.state["session"]):
            raise recovery.FenceError("PRIVATE_BINDING_INVALID")
        self.authorization.remaining()
        self.store._guard(self.clock())

    def _run(self,root,call,provider,*,budget=3):
        try:
            self._admit()
            if self.fence.state["phase"]=="stopped":self.fence.next_operation()
            result=self.adapter.execute(root,call,self.fence,self.authorization,provider,
                                        budget=budget,stop_gate=self.stop_gate)
            if result["classification"]!="confirmed":
                self.closed=True
            return result
        except BaseException:
            self.closed=True
            self.authorization.suspend()
            raise
        finally:
            if type(provider) is credential.PinnedSyntheticProvider:provider.close()

    def preflight(self,method,root,provider):
        try:
            if method not in {"getMe","getWebhookInfo"}:raise api.TransportRefused("PREFLIGHT_INVALID")
            result=self._run(root,self.adapter.prepare(method,{}),provider)
            if result["classification"]=="confirmed":self.preflights.add(method)
            return result
        except BaseException:
            self.closed=True
            self.authorization.suspend()
            raise
        finally:
            if type(provider) is credential.PinnedSyntheticProvider:provider.close()

    def _ready(self):
        self._admit()
        if self.preflights!={"getMe","getWebhookInfo"}:raise api.TransportRefused("PREFLIGHT_REQUIRED")

    def receive(self,root,provider):
        try:
            self._ready()
            offset=self.store.status()["next_offset"]
            call=self.adapter.prepare("getUpdates",dict(offset=offset,limit=20,timeout=0))
            result=self._run(root,call,provider)
            if result["classification"]!="confirmed":return result
            self._admit()
            # B1 commit les rejets minimaux et le curseur dans la même transaction.
            return self.store.receive_batch(result["data"],offset,self.clock())
        except BaseException:
            self.closed=True
            self.authorization.suspend()
            raise
        finally:
            if type(provider) is credential.PinnedSyntheticProvider:provider.close()

    def prepare(self,message_id,snapshot_loader):
        try:
            self._ready()
            return prepare_trial_fixture(self.fence,self.store,message_id,snapshot_loader,self.clock())
        except BaseException:
            self.closed=True
            self.authorization.suspend()
            raise

    def deliver(self,message_id,root,provider):
        """Jonction F2/B2 fermée ; le deliver ScriptedTransport n'est pas élargi."""
        request=None
        try:
            self._ready()
            response=self.store.connection.execute("SELECT response FROM business_results WHERE message_id=?",(message_id,)).fetchone()
            message=self.store.connection.execute("SELECT payload FROM messages WHERE message_id=?",(message_id,)).fetchone()
            if (response is None or message is None
                    or not outbox.trial_response_allowed(json.loads(message[0]),json.loads(response[0]))
                    or not private_router.authenticate(json.loads(message[0]),self.store.config)):
                raise api.TransportRefused("COMMAND_NOT_ALLOWED")
            permission=outbox.SimulationPermission(self.store.binding["bot_id"],self.store.binding["owner_id"],
                self.store.binding["chat_id"],self.store.generation,True,True)
            with self.store._operation():
                now=self.store._guard(self.clock(),permission)
                request=self.store._begin_attempt(message_id,now)
            if type(request) is not outbox.SimulatedRequest:return request
            call=self.adapter.prepare("sendMessage",dict(chat_id=request.chat_id,text=request.text,parse_mode="HTML"))
            try:
                result=self._run(root,call,provider,budget=min(3,request.expires_at-now))
                self.stop_gate.check()
                _bound(self.fence,self.store)
                if result["classification"]=="confirmed":self.authorization.remaining()
            except Exception:
                result={"classification":"uncertain"}
            status,code,remote,delay="uncertain","invalid",None,0
            if (result["classification"]=="confirmed" and type(result.get("data")) is dict
                    and set(result["data"])=={"status","message_id"}
                    and result["data"]["status"]=="confirmed" and base._positive(result["data"]["message_id"])):
                remote=result["data"]["message_id"]
                status,code="confirmed","success"
            elif result["classification"]=="rejected":
                status,code,delay="rejected","rejected",result["retry_after"]
            # Les contraintes B2 vérifient à nouveau la réponse figée et génération.
            with self.store._operation():
                status=self.store._finish_attempt(request,status,code,remote,self.clock(),delay)
            if status!="confirmed":
                self.closed=True
                self.authorization.suspend()
            return status
        except BaseException:
            self.closed=True
            self.authorization.suspend()
            raise
        finally:
            if type(provider) is credential.PinnedSyntheticProvider:provider.close()

    def shutdown(self):
        # Signal OS AVANT le moindre commit. Le secours CLI peut faire ce signal
        # et la terminaison indépendamment du thread SQLite du contrôleur.
        self.closed=True
        if not self.stop_gate.api.SetEvent(self.stop_gate.handle):
            raise private_stop.StopError("ADMISSION_STOP_UNPROVEN")
        job=self.authorization.job
        if job is not None and not job.safe:job.stop()
        if job is not None and (not job.safe or job.final_counts["active"]!=0):
            raise private_stop.StopError("WORKER_STOP_UNPROVEN")
        try:
            with self.store._operation():
                now=self.store._guard(self.clock())
                with self.store._transaction():
                    self.store.connection.execute("UPDATE attempts SET status='uncertain',completed_at=MAX(?,started_at),code='restart' WHERE status='in_flight'",(now,))
                    self.store.connection.execute("UPDATE outbox SET status='uncertain' WHERE status='in_flight'")
                    self.store.connection.execute("UPDATE outbox SET status='cancelled' WHERE status='prepared'")
                    meta=self.store._meta();meta["last_clock"]=now;self.store._save_meta(meta)
            if self.authorization.status()["state"]=="active":self.authorization._save(state="revoked")
        except BaseException:
            self.authorization.suspend()
            raise
        finally:self.stop_gate.close()

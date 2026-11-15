"""F2 : contrats Bot API privés, exécution uniquement au banc local explicite."""
from dataclasses import dataclass
import json
import math

from . import private_contracts as contracts, private_credential as credential
from . import private_store as base, private_trial as trial


class TransportRefused(ValueError):
    classification="refused_before_send"


@dataclass(frozen=True)
class LocalEndpoint:
    port: int
    host: str = "127.0.0.1"
    tls: bool = False

    def __post_init__(self):
        if (self.host!="127.0.0.1" or type(self.port) is not int or not 1<=self.port<=65535
                or type(self.tls) is not bool):
            raise TransportRefused("LOCAL_ENDPOINT_REQUIRED")


@dataclass(frozen=True)
class FixtureBotCall:
    method: str
    payload_json: str


class LocalBotAdapter:
    """Aucune destination distante, retry, découverte ou autorisation réelle.

    Interface F3 proposée : prepare -> execute -> résultat typé à valider et
    committer par B2, jamais une confirmation durable implicite par l'adaptateur.
    """

    def __init__(self,enrollment,target,endpoint=None,*,enabled=False,io_timeout=.5):
        if (type(enrollment) is not trial.ManualFixtureEnrollment
                or type(target) is not credential.PrivateFixtureTarget
                or (target.bot_id,target.generation)!=(enrollment.bot_id,enrollment.deployment_generation)
                or type(enabled) is not bool
                or (endpoint is not None and type(endpoint) is not LocalEndpoint)
                or type(io_timeout) not in (int,float) or not math.isfinite(io_timeout) or not .05<=io_timeout<=3):
            raise TransportRefused("PRIVATE_BINDING_INVALID")
        self.enrollment,self.target,self.endpoint,self.enabled=enrollment,target,endpoint,enabled
        self.io_timeout=io_timeout

    def prepare(self,method,payload):
        if method not in {"getMe","getWebhookInfo","getUpdates","sendMessage"} or type(payload) is not dict:
            raise TransportRefused("METHOD_INVALID")
        valid=False
        if method in {"getMe","getWebhookInfo"}:valid=not payload
        elif method=="getUpdates":
            valid=(set(payload)=={"offset","limit","timeout"}
                and type(payload["offset"]) is int and 0<=payload["offset"]<2**52
                and type(payload["limit"]) is int and 1<=payload["limit"]<=20
                and type(payload["timeout"]) is int and 0<=payload["timeout"]<=5)
        else:
            valid=(set(payload)=={"chat_id","text","parse_mode"}
                and type(payload["chat_id"]) is int and payload["chat_id"]==self.enrollment.chat_id
                and isinstance(payload["text"],str) and 0<len(payload["text"])<=2048
                and payload["parse_mode"]=="HTML")
        if not valid:raise TransportRefused("PAYLOAD_INVALID")
        wire=json.dumps(payload,ensure_ascii=False,separators=(",",":"),allow_nan=False)
        if len(wire.encode("utf-8"))>8192:raise TransportRefused("REQUEST_SIZE")
        return FixtureBotCall(method,wire)

    def write_fixture(self,root,call):
        if not self.enabled or type(self.endpoint) is not LocalEndpoint:
            raise TransportRefused("ADAPTER_DISABLED")
        if type(call) is not FixtureBotCall:raise TransportRefused("CALL_INVALID")
        try:checked=self.prepare(call.method,json.loads(call.payload_json))
        except (ValueError,TypeError):raise TransportRefused("CALL_INVALID") from None
        if checked!=call:raise TransportRefused("CALL_INVALID")
        root=base._fixture_directory(root)
        path=root/"request.json"
        if not root.is_dir() or path.exists():raise TransportRefused("FIXTURE_NOT_FRESH")
        value=dict(mode="request",port=self.endpoint.port,delay=0,tls=self.endpoint.tls,size=1,
            io_timeout=self.io_timeout,api_request=dict(method=call.method,payload=json.loads(call.payload_json)))
        raw=json.dumps(value,ensure_ascii=False)
        if len(raw.encode("utf-8"))>16384:raise TransportRefused("REQUEST_SIZE")
        path.write_text(raw,encoding="utf-8")

    def validate(self,call,result):
        # Aucun corps/description distante dans les exceptions ou le résultat refusé.
        unknown={"classification":"uncertain","code":"RESULT_UNKNOWN"}
        try:
            if (type(call) is not FixtureBotCall or not isinstance(result,dict)
                    or result.get("safe_stop") is not True or result.get("timely") is not True
                    or result.get("active",0)!=0):return unknown
            if self.prepare(call.method,json.loads(call.payload_json))!=call:return unknown
            item=result["result"]
            if item.get("code")!="complete":return unknown
            status=item["http_status"]
            raw=item["body"].encode("utf-8")
            envelope=contracts.decode_envelope(raw)
            if type(status) is int and status in {400,401,403,409,429}:
                if (envelope.get("ok") is False and type(envelope.get("error_code")) is int
                        and envelope["error_code"]==status
                        and set(envelope)<={"ok","error_code","description","parameters"}):
                    params=envelope.get("parameters",{})
                    if not isinstance(params,dict) or set(params)-{"retry_after"}:return unknown
                    delay=params.get("retry_after",0)
                    if type(delay) is not int or not 0<=delay<=86400 or (delay and status!=429):return unknown
                    return {"classification":"rejected","code":status,"retry_after":delay}
                return unknown
            if type(status) is not int or status!=200 or envelope["ok"] is not True:return unknown
            payload=json.loads(call.payload_json)
            if call.method=="sendMessage":
                data=contracts.validate_message(raw,status,self.enrollment.chat_id,payload["text"])
            elif call.method=="getUpdates":
                config=dict(mode="simulation",send_enabled=False,notifications_channel_id=None,
                    owner_user_id=self.enrollment.owner_id,arbitration_private_chat_id=self.enrollment.chat_id)
                data=contracts.validate_updates(raw,config)
            else:
                if set(envelope)!={"ok","result"} or not isinstance(envelope["result"],dict):return unknown
                data=envelope["result"]
                if call.method=="getMe":
                    if type(data.get("id")) is not int or data["id"]!=self.enrollment.bot_id or data.get("is_bot") is not True:return unknown
                    data={"bot_verified":True}
                elif call.method=="getWebhookInfo":
                    if (data.get("url")!="" or type(data.get("pending_update_count")) is not int
                            or data["pending_update_count"]<0):return unknown
                    data={"webhook_absent":True}
            return {"classification":"confirmed","data":data}
        except (ValueError,TypeError,KeyError,AttributeError):return unknown

    def execute(self,root,call,fence,authorization,provider,*,budget=3,stop_gate=None):
        if not self.enabled or type(self.endpoint) is not LocalEndpoint:
            raise TransportRefused("ADAPTER_DISABLED")
        if type(provider) is not credential.PinnedSyntheticProvider:
            raise TransportRefused("PRIVATE_PROVIDER_REQUIRED")
        if type(authorization) is not trial.FixtureTrial or authorization.enrollment!=self.enrollment:
            raise TransportRefused("ENROLLMENT_MISMATCH")
        job=None
        try:
            vault=provider.acquire(self.target)
            self.write_fixture(root,call)
            job=credential.CredentialFixtureJob(root,budget,fence,authorization,vault,
                action="response" if call.method=="sendMessage" else "read",stop_gate=stop_gate)
            result=job.start().wait()
            outcome=self.validate(call,result)
            if stop_gate is not None and stop_gate.requested:
                outcome={"classification":"uncertain","code":"RESULT_UNKNOWN"}
            if outcome["classification"]!="confirmed":authorization.suspend()
            return outcome
        except (TransportRefused,credential.CredentialError,trial.TrialError):
            if job is None or "resumed" not in job.trace:raise TransportRefused("REFUSED_BEFORE_SEND") from None
            return {"classification":"uncertain","code":"RESULT_UNKNOWN"}
        except Exception:
            if job is None or "resumed" not in job.trace:raise TransportRefused("REFUSED_BEFORE_SEND") from None
            return {"classification":"uncertain","code":"RESULT_UNKNOWN"}
        finally:
            if job is not None and not job.safe:job.stop()
            provider.close()

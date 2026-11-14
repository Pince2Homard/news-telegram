"""C5 : intention B2 avant autorité, réponse perdue, aucun raccord réseau réel."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from tools.windows_watcher import private_authority as external, private_integration as integration
from tools.windows_watcher import private_outbox as outbox, private_recovery as recovery
from tools.windows_watcher import private_store as base, private_trial as trial


class AuthorityIntegrationTests(unittest.TestCase):
    def test_confirmed_authority_lost_reply_does_not_confirm_or_replay_local_outbox(self):
        now=1800000000
        generation="11111111-1111-4111-8111-111111111111"
        host="22222222-2222-4222-8222-222222222222"
        trial_id="33333333-3333-4333-8333-333333333333"
        config=dict(mode="simulation",send_enabled=False,notifications_channel_id=None,
                    owner_user_id=12345,arbitration_private_chat_id=12345)
        binding=dict(simulation_only=True,bot_id=54321,owner_id=12345,chat_id=12345,generation=generation)
        operation=external.FixtureOperation(str(uuid4()),host,1,0,"0"*64,"response",1)
        transport=outbox.ScriptedTransport()
        with tempfile.TemporaryDirectory(prefix="phase823-c5-") as temp,ExitStack() as guards:
            for target in ("socket.socket","requests.Session.request",
                           "tools.windows_watcher.telegram_once.read_external_token"):
                guards.enter_context(patch(target,side_effect=AssertionError("real I/O forbidden")))
            root=Path(temp);pc=root/"pc";pc.mkdir()
            def store(**kwargs):
                return outbox.DeliveryStore(pc/"store",config,54321,now,generation=generation,**kwargs)
            def fence(**kwargs):
                return recovery.FixtureFence(pc/"fence",pc/"witness",pc/"store/fixtures.sqlite3",binding,now,**kwargs)
            with store(create=True):pass
            with fence(create=True):pass
            enrollment=trial.ManualFixtureEnrollment(54321,12345,12345,generation,host,trial_id)
            with external.FixtureAuthority(root/"authority",create=True,trial_id=trial_id,
                    host_id=host,now=now,transport=transport) as authority,trial.FixtureTrial(
                    pc/"trial",enrollment,trial.FixturePlan(fixture_enabled=True),create=True) as budget:
                budget.authorize_fixture(approval="reviewed-synthetic-trial",enrollment=enrollment)
                with fence() as session:
                    session.begin();session.touch_store()
                    def lose_reply(stage):
                        if stage=="after_intent_commit":
                            budget.reserve("response")
                            self.assertEqual(authority.execute(operation,now)["classification"],"confirmed")
                            budget.suspend()
                            raise OSError("synthetic lost result")
                    with store(fault=lose_reply) as data:
                        message=dict(message_id=1,date=now,text="/status",chat=dict(id=12345,type="private"),
                                     **{"from":dict(id=12345,is_bot=False)})
                        integration.receive_fixture(session,data,json.dumps(dict(ok=True,
                            result=[dict(update_id=10,message=message)])).encode(),0,now)
                        integration.prepare_fixture(session,data,1,lambda:None,now)
                        local_model=outbox.ScriptedTransport()
                        with self.assertRaises(base.StoreError):
                            data.deliver(1,local_model,now,permission=outbox.SimulationPermission(
                                54321,12345,12345,generation,True))
                        self.assertEqual(local_model.requests,[])
                    # Aucun seal ne prétend résoudre le résultat externe non réconcilié.
                with store() as data:
                    self.assertEqual(data.inspect_outbox(1)["status"],"uncertain")
                    self.assertEqual((data.status()["stored_offset"],data.status()["receipts"]),(11,1))
                    self.assertEqual(data.connection.execute("SELECT count(*) FROM attempts").fetchone()[0],1)
                self.assertEqual(budget.status()["state"],"recovery_required")
                self.assertEqual(authority.inspect(operation.op_id)["classification"],"confirmed")
                with self.assertRaises(external.AuthorityError):authority.execute(operation,now)
                with self.assertRaises(external.AuthorityError):authority.execute(external.FixtureOperation(
                    str(uuid4()),host,1,authority.status()["revision"],"0"*64,"response",1),now)
                self.assertEqual(transport.accepted,[1])
                with fence() as session:
                    with self.assertRaises(recovery.FenceError):session.begin()


if __name__=="__main__":unittest.main()

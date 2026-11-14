"""C2 : raccords de fixtures explicites, sans boucle ni transport d'envoi."""
import json

from . import private_contracts as contracts, private_outbox as outbox
from . import private_qualification as qualification, private_recovery as recovery, private_router, private_store as base


def _bound(fence, store):
    if type(fence) is not recovery.FixtureFence or type(store) not in (outbox.DeliveryStore, qualification.QualifiedStore):
        raise recovery.FenceError("FIXTURE_REQUIRED")
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

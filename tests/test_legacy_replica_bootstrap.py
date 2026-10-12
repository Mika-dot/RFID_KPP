import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from common.replicated_ingest import ReplicaJournal, ReplicatedDelivery
from guardian.net import json_request
from guardian.node import Node


class LegacyReplicaBootstrapTests(unittest.TestCase):
    def test_legacy_peers_block_delivery_until_held_receiver_is_present(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {"PERIMETER_HA_TOKEN":"x"*64}):
            node=Node.__new__(Node)
            node.cfg={"node_id":"perimetr","state_dir":folder,"listen":"127.0.0.1","port":0}
            node.operator_maintenance=True
            node.replica=ReplicaJournal(Path(folder)/"peer.sqlite","perimetr")
            node.fallback=None
            server=node.server()
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                origin=ReplicaJournal(Path(folder)/"origin.sqlite","comparator")
                peers=[{"id":key,"url":"http://legacy-"+key} for key in ("physical","perimetr","comparator")]
                available=False
                def request(url, body):
                    if not available:return 404,{"error":"NotFound"}
                    if "legacy-perimetr" not in url:return 404,{}
                    return json_request("http://127.0.0.1:%d/replica/put"%server.server_port,"x"*64,body=body)
                delivery=ReplicatedDelivery(origin,peers,request=request)
                payload=dict(client_uuid=str(uuid.uuid4()),source_time="2026-10-12T09:51:00",
                    source_sequence=1,connection_epoch="synthetic",antenna=1,rssi=-40.,epc="SYNTHETIC",
                    tid="",time_quality="HOST_CAPTURE_TIME")
                with self.assertRaisesRegex(RuntimeError,"ReplicaQuorumUnavailable"):
                    delivery.ensure("rfid",payload)
                self.assertEqual(1,origin.stats()["pending"])
                available=True;delivery.peer_retry.clear()
                record=delivery.ensure("rfid",payload)
                self.assertEqual(2,origin.ack_count(record))
                self.assertEqual(1,node.replica.stats()["pending"])
                self.assertTrue(node.operator_maintenance)
                self.assertFalse(hasattr(node,"processes"))
            finally:
                server.shutdown();server.server_close();thread.join(timeout=3)

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from common.replicated_ingest import ReplicaJournal, envelope
from guardian.node import Node
from guardian.net import json_request
from tests.test_ha_resilience import rfid_payload


class ReplicaRecoveryCursorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local = Node.__new__(Node)
        self.local.replica = ReplicaJournal(self.root/"local.sqlite", "comparator")
        self.local.archive_replica = Mock()

    def test_pending_first_http_avoids_retained_sent_history(self):
        remote = Node.__new__(Node)
        remote.cfg = {"listen":"127.0.0.1", "port":0}
        remote.replica = ReplicaJournal(self.root/"peer.sqlite", "perimetr")
        for _ in range(125):
            record=envelope("rfid",rfid_payload());remote.replica.put(record)
            remote.replica.sent(record["stream"],record["uuid"],record["digest"])
        current=envelope("rfid",rfid_payload());remote.replica.put(current)
        with patch.dict(os.environ,{"PERIMETER_HA_TOKEN":"test-token"}):
            server=remote.server(); thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                peer={"id":"perimetr", "url":"http://127.0.0.1:"+str(server.server_port)}
                self.local.pull_replica_peer(peer, pending=True, request=json_request)
            finally:
                server.shutdown();server.server_close();thread.join(2)
        self.assertEqual(current,self.local.replica.get("rfid",current["uuid"]))
        self.assertEqual(1,self.local.replica.stats()["records"])
        restarted=ReplicaJournal(self.root/"local.sqlite","comparator")
        self.assertEqual(126,restarted.peer_cursor("perimetr",pending=True))
        self.assertEqual(0,restarted.peer_cursor("perimetr"))

    def test_history_cursor_survives_restart_and_empty_page(self):
        record=envelope("rfid",rfid_payload())
        page={"items":[{"cursor":12,"record":record,"state":"PENDING"}],"cursor":12}
        with patch.dict(os.environ,{"PERIMETER_HA_TOKEN":"test-token"}):
            self.local.pull_replica_peer({"id":"perimetr","url":"http://peer"},request=Mock(return_value=(200,page)))
            self.local.replica=ReplicaJournal(self.root/"local.sqlite","comparator")
            request=Mock(return_value=(200,{"items":[],"cursor":12}))
            self.local.pull_replica_peer({"id":"perimetr","url":"http://peer"},request=request)
        self.assertIn("after=12",request.call_args.args[0])
        self.assertEqual(12,self.local.replica.peer_cursor("perimetr"))

    def test_archive_failure_never_advances_durable_cursor(self):
        record=envelope("rfid",rfid_payload())
        page={"items":[{"cursor":12,"record":record,"state":"PENDING"}],"cursor":12}
        self.local.archive_replica.side_effect=OSError("disk unavailable")
        with patch.dict(os.environ,{"PERIMETER_HA_TOKEN":"test-token"}):
            with self.assertRaises(OSError):
                self.local.pull_replica_peer({"id":"perimetr","url":"http://peer"},request=Mock(return_value=(200,page)))
        self.assertEqual(0,self.local.replica.peer_cursor("perimetr"))
        self.assertEqual(record,self.local.replica.get("rfid",record["uuid"]))

    def test_invalid_page_cannot_skip_unreceived_records(self):
        request=Mock(return_value=(200,{"items":[],"cursor":99}))
        with patch.dict(os.environ,{"PERIMETER_HA_TOKEN":"test-token"}):
            with self.assertRaisesRegex(ValueError,"SkippedRecords"):
                self.local.pull_replica_peer({"id":"perimetr","url":"http://peer"},request=request)
        self.assertEqual(0,self.local.replica.peer_cursor("perimetr"))

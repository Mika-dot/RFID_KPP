import json
import tempfile
import unittest
from pathlib import Path
from deploy.ha.diagnose_fault import clean,collect


class FaultDiagnosisTests(unittest.TestCase):
    def test_known_secrets_identifiers_and_raw_logs_are_filtered(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/"logs").mkdir()
            (root/"release.json").write_text(json.dumps({"current":{"sha":"a"*40},"pending":False,"rejected":["b"*40],"private":"hidden"}))
            (root/"audit.jsonl").write_text('partial\n'+json.dumps({"time":1,"kind":"candidate_business_fault","reason":"ReplicaQuorumUnavailable","password":"hidden"})+'\n')
            (root/"logs/RfidReader.log").write_text("EPC=rawtag\nTCPConnect code=-1 token=topsecret EPC=rawtag\nreader connected password=hidden\n")
            nodes=[{"id":n,"url":"http://"+n} for n in ("physical","perimetr","comparator")]
            cfg={"node_id":"comparator","state_dir":folder,"nodes":nodes}
            env={"PERIMETER_HA_TOKEN":"topsecret","RFID_READER_IP":"192.0.2.1","RFID_DB_CONNECTION":"hidden"}
            def request(url,*args,**kwargs):
                self.assertNotIn("body",kwargs)
                return 200,{"node":url.split('/')[2],"sample_age":1,"fencing_protocol":2,"active":False,"services":{}}
            result=collect(cfg,env,"b"*40,request,journal="bootstrap_rollback Error password=hidden")
            self.assertTrue(result["release"]["target_rejected"])
            self.assertTrue(result["read_only"])
            raw=json.dumps(result)
            for value in ("topsecret","hidden","rawtag"):self.assertNotIn(value,raw)
            self.assertIn("TCPConnect code=-1",raw)
            self.assertIn("ReplicaQuorumUnavailable",raw)

    def test_connection_strings_urls_and_uuid_are_redacted(self):
        value=clean("PWD=hunter2;UID=admin; rtsp://user:pass@host/video EPC=ABC 11111111-2222-3333-4444-555555555555",{})
        for secret in ("hunter2","admin","user:pass","ABC","11111111-2222"):self.assertNotIn(secret,value)

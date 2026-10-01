"""Opt-in real SQL fencing tests. Requires a NEW dedicated database ending _ha_test."""
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from guardian.sql import SqlStore

TABLES = ["RFID_Tags", "RusGuardLogs", "ReelTransitions", "KPP_ReelEvents",
          "KPP_RuntimeState", "KPP_ActiveRfidSessions", "KPP_ProcessingErrors",
          "KPP_EventVideoLinks", "KPP_EventSkudLinks"]


@unittest.skipUnless(os.getenv("PERIMETER_HA_TEST_SQL"), "requires isolated SQL Server test database")
class SqlIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import pyodbc
        cls.pyodbc = pyodbc
        cls.text = os.environ["PERIMETER_HA_TEST_SQL"]
        cls.env = patch.dict(os.environ, {"PERIMETER_HA_SQL":cls.text})
        cls.env.start()
        cls.conn = pyodbc.connect(cls.text, autocommit=True, timeout=3)
        db = cls.conn.execute("SELECT DB_NAME()").fetchone()[0]
        if not db.lower().endswith("_ha_test"):
            cls.conn.close(); cls.env.stop()
            raise RuntimeError("Refusing integration tests outside an isolated *_ha_test database")
        if any(cls.conn.execute("SELECT OBJECT_ID(?)", "dbo."+t).fetchone()[0] for t in TABLES):
            cls.conn.close(); cls.env.stop()
            raise RuntimeError("Use a fresh test database with no Perimeter tables")
        for table in TABLES:
            cls.conn.execute("CREATE TABLE dbo."+table+"(Id int PRIMARY KEY,[Value] int)")
        script=(Path(__file__).parents[1]/"migrations/003_perimeter_ha.sql").read_text()
        for batch in re.split(r"(?im)^GO\s*$",script):
            if batch.strip(): cls.conn.execute(batch)
        cls.conn.execute("UPDATE dbo.KPP_HA_Lease SET Enabled=1 WHERE Id=1")

    @classmethod
    def tearDownClass(cls):
        for table in TABLES:
            cls.conn.execute("DROP TABLE dbo."+table)
        for table in ("KPP_HA_Lease","KPP_HA_Controller","KPP_HA_NodeState"):
            cls.conn.execute("DROP TABLE dbo."+table)
        cls.conn.close(); cls.env.stop()

    def setUp(self):
        self.conn.execute("UPDATE dbo.KPP_HA_Controller SET ExpiresAt=DATEADD(second,-1,SYSUTCDATETIME())")
        self.store=SqlStore("comparator")
        self.assertTrue(self.store.claim_controller())
        self.store.grant("physical")

    def writer(self,node,epoch):
        c=self.pyodbc.connect(self.text,autocommit=True)
        self.addCleanup(c.close)
        c.execute("EXEC sys.sp_set_session_context @key=N'perimeter_node',@value=?,@read_only=1",node)
        c.execute("EXEC sys.sp_set_session_context @key=N'perimeter_epoch',@value=?,@read_only=1",epoch)
        return c

    def test_stale_open_connection_rejected_after_transfer(self):
        old=self.writer("physical",self.store.lease()["epoch"])
        old.execute("INSERT dbo.RFID_Tags VALUES(1,10)")
        self.store.grant("perimetr")
        with self.assertRaises(self.pyodbc.Error):
            old.execute("INSERT dbo.RFID_Tags VALUES(2,20)")
        new=self.writer("perimetr",self.store.lease()["epoch"])
        new.execute("INSERT dbo.RFID_Tags VALUES(3,30)")
        self.assertEqual(2,self.conn.execute("SELECT COUNT(*) FROM dbo.RFID_Tags").fetchone()[0])

    def test_controller_takeover_fences_old_controller(self):
        backup=SqlStore("perimetr")
        self.assertFalse(backup.claim_controller())
        self.conn.execute("UPDATE dbo.KPP_HA_Controller SET ExpiresAt=DATEADD(second,-1,SYSUTCDATETIME())")
        self.assertTrue(backup.claim_controller())
        with self.assertRaisesRegex(RuntimeError,"ControllerLeaseLost"):
            self.store.grant("physical")
        backup.grant("perimetr")

    def test_missing_session_identity_rejected(self):
        with self.assertRaises(self.pyodbc.Error):
            self.conn.execute("INSERT dbo.RusGuardLogs VALUES(100,1)")

    def test_expired_lease_rejected_in_trigger(self):
        writer=self.writer("physical",self.store.lease()["epoch"])
        self.conn.execute("UPDATE dbo.KPP_HA_Lease SET ExpiresAt=DATEADD(second,-1,SYSUTCDATETIME())")
        with self.assertRaises(self.pyodbc.Error):
            writer.execute("INSERT dbo.ReelTransitions VALUES(100,1)")

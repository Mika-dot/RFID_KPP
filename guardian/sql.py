from __future__ import annotations

import os
import uuid
from contextlib import contextmanager


class SqlStore:
    """SQL Server's UTC clock and row locks are the sole lease authority."""

    def __init__(self, controller=None):
        self.controller = controller
        self.token = str(uuid.uuid4())

    @contextmanager
    def connect(self):
        import pyodbc
        conn = pyodbc.connect(os.environ["PERIMETER_HA_SQL"], timeout=3, autocommit=False)
        conn.timeout = 3
        conn.execute("SET LOCK_TIMEOUT 2500; SET XACT_ABORT ON;")
        try:
            yield conn
        finally:
            conn.close()

    def claim_controller(self, ttl=15):
        with self.connect() as conn:
            row = conn.execute("""
UPDATE dbo.KPP_HA_Controller WITH (UPDLOCK,HOLDLOCK)
SET Owner=?, Token=?, ExpiresAt=DATEADD(second,?,SYSUTCDATETIME())
OUTPUT inserted.Token
WHERE Id=1 AND (ExpiresAt<=SYSUTCDATETIME() OR Token=?);
""", self.controller, self.token, ttl, self.token).fetchone()
            conn.commit()
            return bool(row)

    def lease(self):
        with self.connect() as conn:
            row = conn.execute("""
SELECT Owner,Epoch,CASE WHEN Enabled=1 AND ExpiresAt>SYSUTCDATETIME()
THEN 1 ELSE 0 END, DATEDIFF(second,StartedAt,SYSUTCDATETIME()),Enabled
FROM dbo.KPP_HA_Lease WHERE Id=1
""").fetchone()
            if row is None:
                raise RuntimeError("HA schema not installed")
            return {"owner": row[0], "epoch": int(row[1]), "valid": bool(row[2]),
                    "age": int(row[3]), "enabled": bool(row[4])}

    def grant(self, owner, ttl=15):
        with self.connect() as conn:
            controller = conn.execute("""
SELECT Token FROM dbo.KPP_HA_Controller WITH (UPDLOCK,HOLDLOCK)
WHERE Id=1 AND Token=? AND ExpiresAt>SYSUTCDATETIME()
""", self.token).fetchone()
            if controller is None:
                raise RuntimeError("ControllerLeaseLost")
            conn.execute("""
UPDATE dbo.KPP_HA_Lease WITH (UPDLOCK,HOLDLOCK)
SET Epoch=Epoch+CASE WHEN ISNULL(Owner,'')<>ISNULL(?,'') OR
ExpiresAt<=SYSUTCDATETIME() THEN 1 ELSE 0 END,
StartedAt=CASE WHEN ISNULL(Owner,'')<>ISNULL(?,'') OR
ExpiresAt<=SYSUTCDATETIME() THEN SYSUTCDATETIME() ELSE StartedAt END,
Owner=?,ExpiresAt=DATEADD(second,?,SYSUTCDATETIME())
WHERE Id=1 AND Enabled=1
""", owner, owner, owner, ttl if owner else 0)
            conn.commit()

    def node_state(self, node):
        with self.connect() as conn:
            row = conn.execute("""
SELECT Faulted, DATEDIFF(second,VerifiedAt,SYSUTCDATETIME())
FROM dbo.KPP_HA_NodeState WHERE NodeId=?
""", node).fetchone()
            return {"faulted": bool(row[0]), "verified_age": row[1]} if row else {
                "faulted": False, "verified_age": None}

    def fault(self, node):
        with self.connect() as conn:
            conn.execute("""
MERGE dbo.KPP_HA_NodeState WITH(HOLDLOCK) t USING(SELECT ? NodeId)s
ON t.NodeId=s.NodeId WHEN MATCHED THEN UPDATE SET Faulted=1,VerifiedAt=NULL
WHEN NOT MATCHED THEN INSERT(NodeId,Faulted)VALUES(s.NodeId,1);
""", node)
            conn.commit()

    def recovered(self, node):
        with self.connect() as conn:
            conn.execute("""
UPDATE dbo.KPP_HA_NodeState SET Faulted=0,VerifiedAt=SYSUTCDATETIME()
WHERE NodeId=? AND Faulted=1
""", node)
            conn.commit()

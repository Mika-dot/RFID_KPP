from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

FENCING_PROTOCOL = 2
EPOCH_BARRIER = "Perimeter.HA.Epoch"


def epoch_barrier(conn, mode="Exclusive", timeout=2500):
    """Serialize epoch changes with complete writer transactions, not renewals."""
    row = conn.execute("""
IF @@TRANCOUNT=0 BEGIN TRANSACTION;
DECLARE @result int;
EXEC @result=sys.sp_getapplock @Resource=?, @LockMode=?,
 @LockOwner=N'Transaction', @LockTimeout=?;
SELECT @result;
""", EPOCH_BARRIER, mode, timeout).fetchone()
    if row is None or int(row[0]) < 0:
        raise RuntimeError("EpochBarrierUnavailable")


def control_odbc():
    """Configure the Guardian process before pyodbc allocates its first HENV.

    Closing a pooled connection can retain its SQL socket. The control agent's
    short checks must release physical connections, including readiness probes.
    Worker interpreters are separate processes and keep their own ODBC settings.
    """
    import pyodbc
    pyodbc.pooling = False
    return pyodbc


class SqlStore:
    """SQL Server's UTC clock and row locks are the sole lease authority."""

    def __init__(self, controller=None):
        self.controller = controller
        self.token = str(uuid.uuid4())

    @contextmanager
    def connect(self):
        pyodbc = control_odbc()
        conn = pyodbc.connect(os.environ["PERIMETER_HA_SQL"], timeout=3, autocommit=False)
        try:
            conn.timeout = 3
            conn.execute("SET LOCK_TIMEOUT 2500; SET XACT_ABORT ON;")
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
            if row:
                self._renew_sample("controller_renew_jitter", self.controller)
            return bool(row)

    def _renew_sample(self, key, identity):
        import time
        from common.bus_statistics import record
        now = time.monotonic()
        previous = getattr(self, "_renew_samples", {}).get(key)
        if previous and previous[1] == identity and now - previous[0] < .5:
            return  # Extra authority confirmation during a handoff is not a renewal interval.
        if previous and previous[1] == identity:
            record(key, abs(now - previous[0] - 2))
        if not hasattr(self, "_renew_samples"):
            self._renew_samples = {}
        self._renew_samples[key] = (now, identity)

    def lease(self):
        import time
        started = time.monotonic()
        with self.connect() as conn:
            row = conn.execute("""
SELECT Owner,Epoch,CASE WHEN Enabled=1 AND ExpiresAt>SYSUTCDATETIME()
THEN 1 ELSE 0 END, DATEDIFF(second,StartedAt,SYSUTCDATETIME()),Enabled,
CASE WHEN Enabled=1 AND ExpiresAt>SYSUTCDATETIME()
THEN DATEDIFF_BIG(millisecond,SYSUTCDATETIME(),ExpiresAt)/1000.0 ELSE 0 END
FROM dbo.KPP_HA_Lease WHERE Id=1
""").fetchone()
            if row is None:
                raise RuntimeError("HA schema not installed")
            return {"owner": row[0], "epoch": int(row[1]), "valid": bool(row[2]),
                    "age": int(row[3]), "enabled": bool(row[4]),
                    "remaining_sec": max(0, float(row[5]) - (time.monotonic() - started)) if len(row) > 5 else 0}

    def controller_owned(self):
        # Repair must observe ownership, never renew it on behalf of election.
        with self.connect() as conn:
            return conn.execute("""
SELECT Id FROM dbo.KPP_HA_Controller
WHERE Id=1 AND Token=? AND ExpiresAt>SYSUTCDATETIME()
""", self.token).fetchone() is not None

    def controller_status(self):
        with self.connect() as conn:
            row = conn.execute("""SELECT Owner,CASE WHEN ExpiresAt>SYSUTCDATETIME() THEN 1 ELSE 0 END
                FROM dbo.KPP_HA_Controller WHERE Id=1""").fetchone()
            return {"owner": row[0], "valid": bool(row[1])} if row else {"owner": None, "valid": False}

    def grant(self, owner, ttl=15, fence_previous=None):
        # A renewal cannot change Enabled, Owner, Epoch or StartedAt. It needs
        # only a brief row update; taking the epoch barrier would starve it behind
        # a long business transaction and expire an otherwise healthy stack.
        if owner:
            with self.connect() as conn:
                self._grant_authority(conn, owner)
                conn.execute("SELECT Id FROM dbo.KPP_HA_Lease WITH(UPDLOCK,HOLDLOCK) WHERE Id=1").fetchone()
                self._candidate_allowed(conn, owner)
                row = conn.execute("""
UPDATE dbo.KPP_HA_Lease
SET ExpiresAt=DATEADD(second,?,SYSUTCDATETIME())
OUTPUT inserted.Epoch
WHERE Id=1 AND Enabled=1 AND Owner=? AND ExpiresAt>SYSUTCDATETIME()
""", ttl, owner).fetchone()
                if row is not None:
                    conn.commit()
                    self._renew_sample("lease_renew_jitter", owner)
                    return
            # Close the unsuccessful renewal transaction before acquiring the
            # barrier. Writers acquire the barrier before reading the lease.
        with self.connect() as conn:
            epoch_barrier(conn)
            self._grant_authority(conn, owner)
            old = conn.execute("SELECT Epoch FROM dbo.KPP_HA_Lease WITH(UPDLOCK,HOLDLOCK) WHERE Id=1").fetchone()
            if fence_previous:
                if owner is not None:
                    raise ValueError("FenceRequiresDemotion")
                conn.execute("""MERGE dbo.KPP_HA_HardwareFence WITH(HOLDLOCK) t USING(SELECT ? NodeId)s
                    ON t.NodeId=s.NodeId WHEN MATCHED THEN UPDATE SET Pending=1,Epoch=?,VerifiedAt=NULL
                    WHEN NOT MATCHED THEN INSERT(NodeId,Pending,Epoch)VALUES(s.NodeId,1,?);""",
                    fence_previous, int(old[0])+1, int(old[0])+1)
            self._candidate_allowed(conn, owner)
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

    def _grant_authority(self, conn, owner):
        controller = conn.execute("""
SELECT Token FROM dbo.KPP_HA_Controller WITH (UPDLOCK,HOLDLOCK)
WHERE Id=1 AND Token=? AND ExpiresAt>SYSUTCDATETIME()
""", self.token).fetchone()
        if controller is None:
            raise RuntimeError("ControllerLeaseLost")

    def _candidate_allowed(self, conn, owner):
        if owner:
            conn.execute("""IF OBJECT_ID(N'dbo.KPP_HA_HardwareFence',N'U') IS NOT NULL
                EXEC sys.sp_executesql N'IF EXISTS(SELECT 1 FROM dbo.KPP_HA_HardwareFence WHERE Pending=1)
                THROW 51041,''HardwareFencePending'',1;';""")
            blocked = conn.execute("SELECT Faulted FROM dbo.KPP_HA_NodeState WITH(UPDLOCK,HOLDLOCK) WHERE NodeId=?", owner).fetchone()
            if blocked and blocked[0]:
                raise RuntimeError("CandidateInRepair")

    def pending_fences(self):
        with self.connect() as conn:
            return [(str(r[0]), int(r[1])) for r in conn.execute(
                "SELECT NodeId,Epoch FROM dbo.KPP_HA_HardwareFence WHERE Pending=1").fetchall()]

    def confirm_fence(self, node, epoch):
        with self.connect() as conn:
            self._grant_authority(conn, None)
            conn.execute("UPDATE dbo.KPP_HA_HardwareFence SET Pending=0,VerifiedAt=SYSUTCDATETIME() WHERE NodeId=? AND Epoch=? AND Pending=1", node, epoch)
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

    def begin_repair(self, node):
        with self.connect() as conn:
            lease = conn.execute("""
SELECT Owner,CASE WHEN ExpiresAt>SYSUTCDATETIME() AND Enabled=1 THEN 1 ELSE 0 END
FROM dbo.KPP_HA_Lease WITH(UPDLOCK,HOLDLOCK) WHERE Id=1
""").fetchone()
            if lease[0] == node and lease[1]:
                raise RuntimeError("ActiveNodeRepairForbidden")
            conn.execute("""
MERGE dbo.KPP_HA_NodeState WITH(HOLDLOCK) t USING(SELECT ? NodeId)s
ON t.NodeId=s.NodeId WHEN MATCHED THEN UPDATE SET Faulted=1,VerifiedAt=NULL
WHEN NOT MATCHED THEN INSERT(NodeId,Faulted)VALUES(s.NodeId,1);
""", node)
            conn.commit()

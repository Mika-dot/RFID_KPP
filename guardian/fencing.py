"""Install immutable writer identity before any production module is imported."""
from __future__ import annotations

import os


def install():
    node = os.environ.get("PERIMETER_HA_NODE")
    epoch = os.environ.get("PERIMETER_HA_EPOCH")
    if not node and not epoch:
        return
    if not node or not epoch or not epoch.isdigit():
        raise RuntimeError("Invalid HA writer identity")
    import pyodbc
    original = pyodbc.connect
    if getattr(original, "_ha_wrapped", False):
        return

    def connect(*args, **kwargs):
        conn = original(*args, **kwargs)
        try:
            db = conn.execute("SELECT DB_NAME()").fetchone()[0]
            conn.execute("EXEC sys.sp_set_session_context @key=N'perimeter_node',@value=?,@read_only=1", node)
            conn.execute("EXEC sys.sp_set_session_context @key=N'perimeter_epoch',@value=?,@read_only=1", int(epoch))
            # The RusGuard source is read-only. All other connections must point
            # at a fenced output database, otherwise startup fails closed.
            if db != os.getenv("SRC_DATABASE", "RusGuardDB"):
                row = conn.execute("""
SELECT Id FROM dbo.KPP_HA_Lease WHERE Id=1 AND Enabled=1 AND Owner=?
AND Epoch=? AND ExpiresAt>SYSUTCDATETIME()
""", node, int(epoch)).fetchone()
                if row is None:
                    raise RuntimeError("WriterLeaseInvalid")
            conn.commit()
            return conn
        except BaseException:
            conn.close()
            raise
    connect._ha_wrapped = True
    pyodbc.connect = connect

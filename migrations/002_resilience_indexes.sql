/* RFID КПП resilience indexes. Safe to rerun. */
SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

/*
Repeated business-flow checks and report/reconciliation windows filter Warehouse
by Dt. The older Tag,Dt index cannot efficiently serve a Dt-only predicate and
would eventually turn a 15-second health probe into a full-table scan.
*/
IF NOT EXISTS (
    SELECT 1
    FROM sys.indexes
    WHERE object_id=OBJECT_ID('dbo.Warehouse')
      AND name='IX_Warehouse_Dt_Id'
)
    CREATE INDEX IX_Warehouse_Dt_Id ON dbo.Warehouse(Dt, Id);
GO

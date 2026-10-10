/* Optional control ledger. Required ONLY for hardware_fencing_required=true.
   Apply explicitly after all three nodes accept the new Guardian runtime.
   No business data, lease reset, grants or automatic migration. */
SET XACT_ABORT ON;
IF OBJECT_ID(N'dbo.KPP_HA_HardwareFence',N'U') IS NULL
BEGIN
    CREATE TABLE dbo.KPP_HA_HardwareFence(
        NodeId NVARCHAR(100) NOT NULL PRIMARY KEY,
        Pending BIT NOT NULL,
        Epoch BIGINT NOT NULL,
        VerifiedAt DATETIME2(3) NULL
    );
END;
GO

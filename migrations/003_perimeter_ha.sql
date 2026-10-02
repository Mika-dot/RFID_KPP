/* Run with a migration administrator after the existing schema migration.
   SQL Server 2016+; every node MUST use the same database. Enable is separate. */
SET XACT_ABORT ON;
IF OBJECT_ID('dbo.KPP_HA_Lease','U') IS NULL
BEGIN
 CREATE TABLE dbo.KPP_HA_Lease(
 Id int PRIMARY KEY CHECK(Id=1), Enabled bit NOT NULL DEFAULT(0),
 Owner nvarchar(64) NULL, Epoch bigint NOT NULL DEFAULT(0),
 StartedAt datetime2 NOT NULL DEFAULT(SYSUTCDATETIME()),
 ExpiresAt datetime2 NOT NULL DEFAULT(SYSUTCDATETIME()));
 INSERT dbo.KPP_HA_Lease(Id) VALUES(1);
END;
IF OBJECT_ID('dbo.KPP_HA_Controller','U') IS NULL
BEGIN
 CREATE TABLE dbo.KPP_HA_Controller(
 Id int PRIMARY KEY CHECK(Id=1),Owner nvarchar(64) NULL,
 Token nvarchar(64) NULL,ExpiresAt datetime2 NOT NULL DEFAULT(SYSUTCDATETIME()));
 INSERT dbo.KPP_HA_Controller(Id) VALUES(1);
END;
IF OBJECT_ID('dbo.KPP_HA_NodeState','U') IS NULL
 CREATE TABLE dbo.KPP_HA_NodeState(
 NodeId nvarchar(64) PRIMARY KEY,Faulted bit NOT NULL DEFAULT(0),VerifiedAt datetime2 NULL);
GO
/* Guard only Perimeter-owned outputs; 1C inputs Warehouse/RfidTags stay writable.
   UPDLOCK + HOLDLOCK keeps lease validation and business commit serialized with
   leadership changes. A stale process cannot commit after a new epoch is granted. */
DECLARE @table sysname,@sql nvarchar(max);
DECLARE tables_cursor CURSOR LOCAL FAST_FORWARD FOR
 SELECT name FROM (VALUES ('RFID_Tags'),('RusGuardLogs'),('ReelTransitions'),
 ('KPP_ReelEvents'),('KPP_RuntimeState'),('KPP_ActiveRfidSessions'),
 ('KPP_ProcessingErrors'),('KPP_EventVideoLinks'),('KPP_EventSkudLinks')) t(name);
OPEN tables_cursor;
FETCH NEXT FROM tables_cursor INTO @table;
WHILE @@FETCH_STATUS=0
BEGIN
 IF OBJECT_ID('dbo.'+@table,'U') IS NULL
 BEGIN
  CLOSE tables_cursor; DEALLOCATE tables_cursor;
  THROW 51003,'Required Perimeter output table missing; run existing migration first',1;
 END;
 SET @sql=N'CREATE OR ALTER TRIGGER dbo.'+QUOTENAME('HA_'+@table)+N'
 ON dbo.'+QUOTENAME(@table)+N' AFTER INSERT,UPDATE,DELETE AS
 BEGIN
 SET NOCOUNT ON;
 DECLARE @enabled bit,@owner nvarchar(64),@epoch bigint,@expires datetime2;
 SELECT @enabled=Enabled,@owner=Owner,@epoch=Epoch,@expires=ExpiresAt
 FROM dbo.KPP_HA_Lease WITH(UPDLOCK,HOLDLOCK) WHERE Id=1;
 IF @enabled=0 RETURN;
 IF @enabled IS NULL OR @owner IS NULL OR @expires<=SYSUTCDATETIME()
 OR ISNULL(CONVERT(nvarchar(64),SESSION_CONTEXT(N''perimeter_node'')),N'''')<>@owner
 OR ISNULL(TRY_CONVERT(bigint,SESSION_CONTEXT(N''perimeter_epoch'')),-1)<>@epoch
 THROW 51001,''Perimeter write fenced: no current lease'',1;
 END;';
 EXEC sys.sp_executesql @sql;
 FETCH NEXT FROM tables_cursor INTO @table;
END;
CLOSE tables_cursor;DEALLOCATE tables_cursor;

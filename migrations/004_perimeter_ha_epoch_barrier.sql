/* Apply only with ALL controllers on fencing protocol 2 and every worker stopped.
   Run the whole batch in one administrator transaction. Keep HA enabled.
   A transaction-owned Shared application lock fences business commits against
   an Exclusive epoch change. Reading the lease uses READ COMMITTED; renewals
   update only ExpiresAt and do not acquire the epoch barrier. */
SET XACT_ABORT ON;
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
  THROW 51003,'Required Perimeter output table missing',1;
 END;
 SET @sql=N'CREATE OR ALTER TRIGGER dbo.'+QUOTENAME('HA_'+@table)+N'
 ON dbo.'+QUOTENAME(@table)+N' AFTER INSERT,UPDATE,DELETE AS
 BEGIN
 SET NOCOUNT ON;
 DECLARE @lock int,@enabled bit,@owner nvarchar(64),@epoch bigint,@expires datetime2;
 EXEC @lock=sys.sp_getapplock @Resource=N''Perimeter.HA.Epoch'',
  @LockMode=N''Shared'',@LockOwner=N''Transaction'',@LockTimeout=2500;
 IF @lock<0 THROW 51004,''Perimeter epoch barrier unavailable'',1;
 SELECT @enabled=Enabled,@owner=Owner,@epoch=Epoch,@expires=ExpiresAt
 FROM dbo.KPP_HA_Lease WITH(READCOMMITTEDLOCK) WHERE Id=1;
 IF @enabled=0 RETURN;
 IF @enabled IS NULL OR @owner IS NULL OR @expires<=SYSUTCDATETIME()
 OR ISNULL(CONVERT(nvarchar(64),SESSION_CONTEXT(N''perimeter_node'')),N'''')<>@owner
 OR ISNULL(TRY_CONVERT(bigint,SESSION_CONTEXT(N''perimeter_epoch'')),-1)<>@epoch
 THROW 51001,''Perimeter write fenced: no current lease'',1;
 END;';
 EXEC sys.sp_executesql @sql;
 FETCH NEXT FROM tables_cursor INTO @table;
END;
CLOSE tables_cursor; DEALLOCATE tables_cursor;

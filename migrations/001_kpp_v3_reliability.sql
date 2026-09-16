/* RFID КПП v3: миграция надёжности. Выполнять один раз под учётной записью с ALTER. */
SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

/* One-time safety snapshots before legacy rows are reclassified. */
IF OBJECT_ID('dbo.KPP_ReelEvents_PreV3_Backup','U') IS NULL
BEGIN
    SELECT * INTO dbo.KPP_ReelEvents_PreV3_Backup FROM dbo.KPP_ReelEvents;
END;
IF OBJECT_ID('dbo.KPP_RuntimeState_PreV3_Backup','U') IS NULL AND OBJECT_ID('dbo.KPP_RuntimeState','U') IS NOT NULL
BEGIN
    SELECT * INTO dbo.KPP_RuntimeState_PreV3_Backup FROM dbo.KPP_RuntimeState;
END;
GO

/* Сырые RFID: отдельно время источника и получения. */
IF COL_LENGTH('dbo.RFID_Tags', 'ClientReadUuid') IS NULL
    ALTER TABLE dbo.RFID_Tags ADD ClientReadUuid UNIQUEIDENTIFIER NULL;
IF COL_LENGTH('dbo.RFID_Tags', 'ReceivedAt') IS NULL
    ALTER TABLE dbo.RFID_Tags ADD ReceivedAt DATETIME2(3) NULL;
IF COL_LENGTH('dbo.RFID_Tags', 'SourceReaderTime') IS NULL
    ALTER TABLE dbo.RFID_Tags ADD SourceReaderTime DATETIME2(3) NULL;
IF COL_LENGTH('dbo.RFID_Tags', 'SourceSequence') IS NULL
    ALTER TABLE dbo.RFID_Tags ADD SourceSequence BIGINT NULL;
IF COL_LENGTH('dbo.RFID_Tags', 'IngestBatchId') IS NULL
    ALTER TABLE dbo.RFID_Tags ADD IngestBatchId UNIQUEIDENTIFIER NULL;
IF COL_LENGTH('dbo.RFID_Tags', 'TimeQuality') IS NULL
    ALTER TABLE dbo.RFID_Tags ADD TimeQuality VARCHAR(32) NULL;
GO
UPDATE dbo.RFID_Tags
SET ReceivedAt = COALESCE(ReceivedAt, RecordTime),
    SourceReaderTime = COALESCE(SourceReaderTime, RecordTime),
    TimeQuality = COALESCE(TimeQuality, 'LEGACY_RECORD_TIME')
WHERE ReceivedAt IS NULL OR SourceReaderTime IS NULL OR TimeQuality IS NULL;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.RFID_Tags') AND name='UX_RFID_Tags_ClientReadUuid')
    CREATE UNIQUE INDEX UX_RFID_Tags_ClientReadUuid ON dbo.RFID_Tags(ClientReadUuid) WHERE ClientReadUuid IS NOT NULL;
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.RFID_Tags') AND name='IX_RFID_Tags_Id_SourceTime')
    CREATE INDEX IX_RFID_Tags_Id_SourceTime ON dbo.RFID_Tags(Id) INCLUDE(SourceReaderTime,ReceivedAt,Antenna,RSSI,EPC,TID,TimeQuality);
GO

/* Видео: нормальный бинарный JPEG, capture/processing time, UUID и число катушек. */
IF COL_LENGTH('dbo.ReelTransitions', 'ClientEventUuid') IS NULL
    ALTER TABLE dbo.ReelTransitions ADD ClientEventUuid UNIQUEIDENTIFIER NULL;
IF COL_LENGTH('dbo.ReelTransitions', 'CapturedAt') IS NULL
    ALTER TABLE dbo.ReelTransitions ADD CapturedAt DATETIME2(3) NULL;
IF COL_LENGTH('dbo.ReelTransitions', 'ProcessedAt') IS NULL
    ALTER TABLE dbo.ReelTransitions ADD ProcessedAt DATETIME2(3) NULL;
IF COL_LENGTH('dbo.ReelTransitions', 'ReceivedAt') IS NULL
    ALTER TABLE dbo.ReelTransitions ADD ReceivedAt DATETIME2(3) NULL;
IF COL_LENGTH('dbo.ReelTransitions', 'ImageData') IS NULL
    ALTER TABLE dbo.ReelTransitions ADD ImageData VARBINARY(MAX) NULL;
IF COL_LENGTH('dbo.ReelTransitions', 'ReelCount') IS NULL
    ALTER TABLE dbo.ReelTransitions ADD ReelCount INT NULL;
IF COL_LENGTH('dbo.ReelTransitions', 'SourceTrackIds') IS NULL
    ALTER TABLE dbo.ReelTransitions ADD SourceTrackIds NVARCHAR(512) NULL;
IF COL_LENGTH('dbo.ReelTransitions', 'TimeQuality') IS NULL
    ALTER TABLE dbo.ReelTransitions ADD TimeQuality VARCHAR(32) NULL;
GO
UPDATE dbo.ReelTransitions
SET CapturedAt = COALESCE(CapturedAt, [Timestamp]),
    ProcessedAt = COALESCE(ProcessedAt, [Timestamp]),
    ReceivedAt = COALESCE(ReceivedAt, [Timestamp]),
    ReelCount = COALESCE(ReelCount, 1),
    TimeQuality = COALESCE(TimeQuality, 'LEGACY_TIMESTAMP')
WHERE CapturedAt IS NULL OR ProcessedAt IS NULL OR ReceivedAt IS NULL OR ReelCount IS NULL OR TimeQuality IS NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.ReelTransitions') AND name='UX_ReelTransitions_ClientEventUuid')
    CREATE UNIQUE INDEX UX_ReelTransitions_ClientEventUuid ON dbo.ReelTransitions(ClientEventUuid) WHERE ClientEventUuid IS NOT NULL;
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.ReelTransitions') AND name='IX_ReelTransitions_CapturedAt')
    CREATE INDEX IX_ReelTransitions_CapturedAt ON dbo.ReelTransitions(CapturedAt, Id) INCLUDE(Direction, TransportMode, ReelCount, ClientEventUuid);
GO

/* СКУД: время события не смешивается со временем доставки. */
IF COL_LENGTH('dbo.RusGuardLogs', 'ReceivedAt') IS NULL
    ALTER TABLE dbo.RusGuardLogs ADD ReceivedAt DATETIME2(3) NULL;
GO
UPDATE dbo.RusGuardLogs SET ReceivedAt=COALESCE(ReceivedAt, CreatedAt) WHERE ReceivedAt IS NULL;
GO

/* Итоговые события: катушка отделена от любого RFID-объекта. */
IF COL_LENGTH('dbo.KPP_ReelEvents', 'IsReel') IS NULL
    ALTER TABLE dbo.KPP_ReelEvents ADD IsReel BIT NOT NULL CONSTRAINT DF_KPP_ReelEvents_IsReel DEFAULT(0);
IF COL_LENGTH('dbo.KPP_ReelEvents', 'ObjectType') IS NULL
    ALTER TABLE dbo.KPP_ReelEvents ADD ObjectType VARCHAR(32) NOT NULL CONSTRAINT DF_KPP_ReelEvents_ObjectType DEFAULT('UNKNOWN_RFID');
IF COL_LENGTH('dbo.KPP_ReelEvents', 'ReelClassification') IS NULL
    ALTER TABLE dbo.KPP_ReelEvents ADD ReelClassification VARCHAR(64) NULL;
IF COL_LENGTH('dbo.KPP_ReelEvents', 'WarehouseId') IS NULL
    ALTER TABLE dbo.KPP_ReelEvents ADD WarehouseId BIGINT NULL;
IF COL_LENGTH('dbo.KPP_ReelEvents', 'WarehouseDt') IS NULL
    ALTER TABLE dbo.KPP_ReelEvents ADD WarehouseDt DATETIME2(3) NULL;
IF COL_LENGTH('dbo.KPP_ReelEvents', 'WarehouseDocIds') IS NULL
    ALTER TABLE dbo.KPP_ReelEvents ADD WarehouseDocIds NVARCHAR(128) NULL;
IF COL_LENGTH('dbo.KPP_ReelEvents', 'PassageGroupKey') IS NULL
    ALTER TABLE dbo.KPP_ReelEvents ADD PassageGroupKey CHAR(32) NULL;
IF COL_LENGTH('dbo.KPP_ReelEvents', 'GroupReelCount') IS NULL
    ALTER TABLE dbo.KPP_ReelEvents ADD GroupReelCount INT NULL;
IF COL_LENGTH('dbo.KPP_ReelEvents', 'RfidMinRawId') IS NULL
    ALTER TABLE dbo.KPP_ReelEvents ADD RfidMinRawId BIGINT NULL;
IF COL_LENGTH('dbo.KPP_ReelEvents', 'RfidMaxRawId') IS NULL
    ALTER TABLE dbo.KPP_ReelEvents ADD RfidMaxRawId BIGINT NULL;
IF COL_LENGTH('dbo.KPP_ReelEvents', 'SourceTimeQuality') IS NULL
    ALTER TABLE dbo.KPP_ReelEvents ADD SourceTimeQuality VARCHAR(32) NULL;
IF COL_LENGTH('dbo.KPP_ReelEvents', 'ProcessingVersion') IS NULL
    ALTER TABLE dbo.KPP_ReelEvents ADD ProcessingVersion VARCHAR(32) NULL;
IF COL_LENGTH('dbo.KPP_ReelEvents', 'VideoClientEventUuid') IS NULL
    ALTER TABLE dbo.KPP_ReelEvents ADD VideoClientEventUuid UNIQUEIDENTIFIER NULL;
GO

/* dbo.Warehouse.Id is BIGINT; keep the persisted relation lossless. */
IF EXISTS (
    SELECT 1
    FROM sys.columns c
    JOIN sys.types t ON t.user_type_id=c.user_type_id
    WHERE c.object_id=OBJECT_ID('dbo.KPP_ReelEvents')
      AND c.name='WarehouseId'
      AND t.name='int'
)
    ALTER TABLE dbo.KPP_ReelEvents ALTER COLUMN WarehouseId BIGINT NULL;
GO

/* Историческая первичная классификация: только точная полная метка. */
UPDATE e
SET IsReel=1,
    ObjectType='REEL',
    ReelClassification=CASE
        WHEN t.Id IS NOT NULL AND w.Id IS NOT NULL THEN 'FULL_TAG_BOTH'
        WHEN t.Id IS NOT NULL THEN 'FULL_TAG_1C'
        ELSE 'FULL_TAG_WAREHOUSE'
    END,
    Task1CId=t.Id,
    Task1CDt=t.Dt,
    Task1CDocIds=t.Ids,
    TaskMatchType=CASE WHEN t.Id IS NOT NULL THEN CASE WHEN w.Id IS NOT NULL THEN 'FULL_TAG_BOTH' ELSE 'FULL_TAG_1C' END ELSE 'FULL_TAG_WAREHOUSE' END,
    WarehouseId=w.Id,
    WarehouseDt=w.Dt,
    WarehouseDocIds=w.Ids,
    ProcessingVersion='LEGACY_BACKFILL_V3'
FROM dbo.KPP_ReelEvents e
OUTER APPLY (SELECT TOP(1) r.Id, r.Dt, r.Ids FROM dbo.RfidTags r WHERE UPPER(LTRIM(RTRIM(r.Tag)))=UPPER(LTRIM(RTRIM(e.SourceTag))) AND ABS(DATEDIFF(MINUTE,r.Dt,e.FirstSeen))<=1440 ORDER BY ABS(DATEDIFF(SECOND,r.Dt,e.FirstSeen)), r.Id DESC) t
OUTER APPLY (SELECT TOP(1) w0.Id, w0.Dt, w0.Ids FROM dbo.Warehouse w0 WHERE UPPER(LTRIM(RTRIM(w0.Tag)))=UPPER(LTRIM(RTRIM(e.SourceTag))) AND ABS(DATEDIFF(MINUTE,w0.Dt,e.FirstSeen))<=1440 ORDER BY ABS(DATEDIFF(SECOND,w0.Dt,e.FirstSeen)), w0.Id DESC) w
WHERE (t.Id IS NOT NULL OR w.Id IS NOT NULL)
  AND (e.ProcessingVersion IS NULL OR e.ProcessingVersion NOT LIKE '3.%');
GO

/* Legacy RFID без точного подтверждения не должен показывать чужую 1С-ссылку. */
UPDATE dbo.KPP_ReelEvents
SET Task1CId=NULL, Task1CDt=NULL, Task1CDocIds=NULL, TaskMatchType='NOT_REEL',
    WarehouseId=NULL, WarehouseDt=NULL, WarehouseDocIds=NULL,
    ObjectType='UNKNOWN_RFID', ReelClassification='NOT_CONFIRMED',
    ProcessingVersion='LEGACY_BACKFILL_V3'
WHERE IsReel=0 AND ISNULL(RfidReadCount,0)>0
  AND (ProcessingVersion IS NULL OR ProcessingVersion NOT LIKE '3.%');
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.KPP_ReelEvents') AND name='IX_KPP_ReelEvents_IsReel_FirstSeen')
    CREATE INDEX IX_KPP_ReelEvents_IsReel_FirstSeen ON dbo.KPP_ReelEvents(IsReel, FirstSeen DESC) INCLUDE(SourceTag, EventKey, FinalDirection, VideoEventId, NeedRecheck, ObjectType, ReelClassification);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.KPP_ReelEvents') AND name='IX_KPP_ReelEvents_RawIdRange')
    CREATE INDEX IX_KPP_ReelEvents_RawIdRange ON dbo.KPP_ReelEvents(RfidMinRawId, RfidMaxRawId) INCLUDE(EventKey, SourceTag);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.KPP_ReelEvents') AND name='IX_KPP_ReelEvents_NeedRecheck_FirstSeen')
    CREATE INDEX IX_KPP_ReelEvents_NeedRecheck_FirstSeen ON dbo.KPP_ReelEvents(NeedRecheck, FirstSeen) INCLUDE(EventKey,SourceTag,IsReel,ObjectType,PassageGroupKey,RfidMinRawId,RfidMaxRawId);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.RfidTags') AND name='IX_RfidTags_Tag_Dt')
    CREATE INDEX IX_RfidTags_Tag_Dt ON dbo.RfidTags(Tag, Dt) INCLUDE(Id,Ids,SeriesNumber);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.RfidTags') AND name='IX_RfidTags_Ids_Dt')
   AND EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('dbo.RfidTags') AND name='Ids' AND max_length BETWEEN 1 AND 900)
    CREATE INDEX IX_RfidTags_Ids_Dt ON dbo.RfidTags(Ids, Dt) INCLUDE(Id,Tag,SeriesNumber);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.RfidTags') AND name='IX_RfidTags_SeriesNumber_Dt')
   AND EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('dbo.RfidTags') AND name='SeriesNumber' AND max_length BETWEEN 1 AND 900)
    CREATE INDEX IX_RfidTags_SeriesNumber_Dt ON dbo.RfidTags(SeriesNumber, Dt) INCLUDE(Id,Tag,Ids);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.Warehouse') AND name='IX_Warehouse_Tag_Dt')
    CREATE INDEX IX_Warehouse_Tag_Dt ON dbo.Warehouse(Tag, Dt) INCLUDE(Id,Ids,SeriesNumber);
GO

/* Durable state активных сессий: checkpoint не теряет накопленный пакет. */
IF OBJECT_ID('dbo.KPP_ActiveRfidSessions','U') IS NULL
BEGIN
    CREATE TABLE dbo.KPP_ActiveRfidSessions(
        SourceTag NVARCHAR(192) NOT NULL CONSTRAINT PK_KPP_ActiveRfidSessions PRIMARY KEY,
        SessionJson NVARCHAR(MAX) NOT NULL,
        MinRawId BIGINT NOT NULL,
        MaxRawId BIGINT NOT NULL,
        FirstSeen DATETIME2(3) NOT NULL,
        LastSeen DATETIME2(3) NOT NULL,
        UpdatedAt DATETIME2(3) NOT NULL CONSTRAINT DF_KPP_ActiveRfidSessions_UpdatedAt DEFAULT SYSDATETIME()
    );
END;
GO

/* Одно внешнее событие принадлежит одной физической группе прохода. */
IF OBJECT_ID('dbo.KPP_EventVideoLinks','U') IS NULL
BEGIN
    CREATE TABLE dbo.KPP_EventVideoLinks(
        PassageGroupKey CHAR(32) NOT NULL CONSTRAINT PK_KPP_EventVideoLinks PRIMARY KEY,
        VideoEventId BIGINT NOT NULL CONSTRAINT UQ_KPP_EventVideoLinks_Video UNIQUE,
        ReelCount INT NOT NULL,
        LinkedAt DATETIME2(3) NOT NULL CONSTRAINT DF_KPP_EventVideoLinks_LinkedAt DEFAULT SYSDATETIME()
    );
END;
IF OBJECT_ID('dbo.KPP_EventSkudLinks','U') IS NULL
BEGIN
    CREATE TABLE dbo.KPP_EventSkudLinks(
        PassageGroupKey CHAR(32) NOT NULL CONSTRAINT PK_KPP_EventSkudLinks PRIMARY KEY,
        SkudExternalId BIGINT NOT NULL CONSTRAINT UQ_KPP_EventSkudLinks_Skud UNIQUE,
        LinkedAt DATETIME2(3) NOT NULL CONSTRAINT DF_KPP_EventSkudLinks_LinkedAt DEFAULT SYSDATETIME()
    );
END;
GO

IF OBJECT_ID('dbo.KPP_ProcessingErrors','U') IS NULL
BEGIN
    CREATE TABLE dbo.KPP_ProcessingErrors(
        ErrorId BIGINT IDENTITY(1,1) PRIMARY KEY,
        ServiceName VARCHAR(64) NOT NULL,
        SourceKey NVARCHAR(256) NULL,
        ErrorText NVARCHAR(2000) NOT NULL,
        PayloadJson NVARCHAR(MAX) NULL,
        CreatedAt DATETIME2(3) NOT NULL DEFAULT SYSDATETIME(),
        ResolvedAt DATETIME2(3) NULL
    );
END;
GO

/* Runtime state мог отсутствовать в старой установке. */
IF OBJECT_ID(N'dbo.KPP_RuntimeState', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.KPP_RuntimeState(
        StateKey NVARCHAR(100) NOT NULL CONSTRAINT PK_KPP_RuntimeState PRIMARY KEY,
        StateValue NVARCHAR(MAX) NULL,
        UpdatedAt DATETIME2(3) NOT NULL CONSTRAINT DF_KPP_RuntimeState_UpdatedAt DEFAULT SYSDATETIME()
    );
END;
GO

/* Безопасный cutover: первый запуск v3 не должен повторно создать всю legacy-историю. */
IF NOT EXISTS (SELECT 1 FROM dbo.KPP_RuntimeState WHERE StateKey='LAST_RFID_ID_V3')
BEGIN
    INSERT INTO dbo.KPP_RuntimeState(StateKey,StateValue,UpdatedAt)
    SELECT 'LAST_RFID_ID_V3', CONVERT(nvarchar(100),COALESCE(MAX(Id),0)), SYSDATETIME()
    FROM dbo.RFID_Tags;
END;
IF NOT EXISTS (SELECT 1 FROM dbo.KPP_RuntimeState WHERE StateKey='KPP_V3_CUTOVER')
BEGIN
    INSERT INTO dbo.KPP_RuntimeState(StateKey,StateValue,UpdatedAt)
    SELECT 'KPP_V3_CUTOVER',
           CONCAT('{"raw_id":',COALESCE(MAX(Id),0),',"at":"',CONVERT(varchar(33),SYSDATETIME(),126),'"}'),
           SYSDATETIME()
    FROM dbo.RFID_Tags;
END;
GO

/* Schema marker used by the one-click launcher. */
MERGE dbo.KPP_RuntimeState AS target
USING (SELECT N'KPP_SCHEMA_VERSION' AS StateKey, N'3.4.5' AS StateValue) AS source
ON target.StateKey=source.StateKey
WHEN MATCHED THEN UPDATE SET StateValue=source.StateValue, UpdatedAt=SYSDATETIME()
WHEN NOT MATCHED THEN INSERT(StateKey,StateValue,UpdatedAt) VALUES(source.StateKey,source.StateValue,SYSDATETIME());
GO

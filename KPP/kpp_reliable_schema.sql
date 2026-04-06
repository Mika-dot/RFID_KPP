/*
  SQL Server schema for reliable KPP reel monitoring.
  Создает staging-таблицу событий и таблицу runtime state.
*/

SET NOCOUNT ON;

IF OBJECT_ID(N'dbo.KPP_RuntimeState', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.KPP_RuntimeState
    (
        StateKey    NVARCHAR(100) NOT NULL CONSTRAINT PK_KPP_RuntimeState PRIMARY KEY,
        StateValue  NVARCHAR(MAX) NULL,
        UpdatedAt   DATETIME2(3) NOT NULL CONSTRAINT DF_KPP_RuntimeState_UpdatedAt DEFAULT SYSDATETIME()
    );
END;
GO

IF OBJECT_ID(N'dbo.KPP_ReelEvents', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.KPP_ReelEvents
    (
        EventId                BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_KPP_ReelEvents PRIMARY KEY,
        EventKey               CHAR(32) NOT NULL,
        SourceTag              NVARCHAR(128) NOT NULL,
        EPC                    NVARCHAR(64) NOT NULL,
        TID                    NVARCHAR(128) NULL,

        Task1CId               INT NULL,
        Task1CDt               DATETIME2(3) NULL,
        Task1CDocIds           NVARCHAR(128) NULL,
        TaskMatchType          VARCHAR(32) NULL,

        FirstSeen              DATETIME2(3) NOT NULL,
        LastSeen               DATETIME2(3) NOT NULL,
        CompletedAt            DATETIME2(3) NULL,
        SessionCloseReason     VARCHAR(32) NULL,

        RfidReadCount          INT NOT NULL CONSTRAINT DF_KPP_ReelEvents_RfidReadCount DEFAULT (0),
        DistinctAntennaCount   INT NOT NULL CONSTRAINT DF_KPP_ReelEvents_DistinctAntennaCount DEFAULT (0),
        DistinctZoneCount      INT NOT NULL CONSTRAINT DF_KPP_ReelEvents_DistinctZoneCount DEFAULT (0),
        RfidFirstAntenna       INT NULL,
        RfidLastAntenna        INT NULL,
        FirstZone              VARCHAR(16) NULL,
        LastZone               VARCHAR(16) NULL,
        RfidAntennasCsv        NVARCHAR(128) NULL,
        RfidZonesCsv           NVARCHAR(64) NULL,
        AvgRSSI                DECIMAL(8,2) NULL,
        MinRSSI                DECIMAL(8,2) NULL,
        MaxRSSI                DECIMAL(8,2) NULL,
        DurationMs             INT NULL,
        RfidDirection          VARCHAR(16) NULL,
        RfidDirectionScore     INT NOT NULL CONSTRAINT DF_KPP_ReelEvents_RfidDirectionScore DEFAULT (0),

        VideoMatched           BIT NOT NULL CONSTRAINT DF_KPP_ReelEvents_VideoMatched DEFAULT (0),
        VideoEventId           BIGINT NULL,
        VideoTime              DATETIME2(3) NULL,
        VideoDirection         VARCHAR(32) NULL,
        VideoTransport         VARCHAR(64) NULL,
        VideoTimeDeltaMs       INT NULL,
        VideoScore             INT NOT NULL CONSTRAINT DF_KPP_ReelEvents_VideoScore DEFAULT (0),

        SkudMatched            BIT NOT NULL CONSTRAINT DF_KPP_ReelEvents_SkudMatched DEFAULT (0),
        SkudExternalId         BIGINT NULL,
        SkudTime               DATETIME2(3) NULL,
        SkudDirection          VARCHAR(32) NULL,
        SkudGate               NVARCHAR(256) NULL,
        SkudPerson             NVARCHAR(256) NULL,
        SkudCard               NVARCHAR(128) NULL,
        SkudTimeDeltaMs        INT NULL,
        SkudScore              INT NOT NULL CONSTRAINT DF_KPP_ReelEvents_SkudScore DEFAULT (0),

        FinalDirection         VARCHAR(16) NOT NULL CONSTRAINT DF_KPP_ReelEvents_FinalDirection DEFAULT ('UNKNOWN'),
        ConfidencePct          INT NOT NULL CONSTRAINT DF_KPP_ReelEvents_ConfidencePct DEFAULT (0),
        ConsensusCode          VARCHAR(32) NOT NULL CONSTRAINT DF_KPP_ReelEvents_ConsensusCode DEFAULT ('NO_DATA'),
        ScoreIn                INT NOT NULL CONSTRAINT DF_KPP_ReelEvents_ScoreIn DEFAULT (0),
        ScoreOut               INT NOT NULL CONSTRAINT DF_KPP_ReelEvents_ScoreOut DEFAULT (0),
        SourceCount            INT NOT NULL CONSTRAINT DF_KPP_ReelEvents_SourceCount DEFAULT (0),
        TransportMode          VARCHAR(32) NOT NULL CONSTRAINT DF_KPP_ReelEvents_TransportMode DEFAULT ('UNKNOWN'),

        WarningFlags           NVARCHAR(1000) NULL,
        EvidenceJson           NVARCHAR(MAX) NULL,

        NeedRecheck            BIT NOT NULL CONSTRAINT DF_KPP_ReelEvents_NeedRecheck DEFAULT (1),
        NextRecheckAt          DATETIME2(3) NULL,
        RecheckCount           INT NOT NULL CONSTRAINT DF_KPP_ReelEvents_RecheckCount DEFAULT (0),
        LastRecheckAt          DATETIME2(3) NULL,

        CreatedAt              DATETIME2(3) NOT NULL CONSTRAINT DF_KPP_ReelEvents_CreatedAt DEFAULT SYSDATETIME(),
        UpdatedAt              DATETIME2(3) NOT NULL CONSTRAINT DF_KPP_ReelEvents_UpdatedAt DEFAULT SYSDATETIME(),
        FinalizedAt            DATETIME2(3) NULL,

        CONSTRAINT UQ_KPP_ReelEvents_EventKey UNIQUE (EventKey)
    );
END;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes WHERE name = N'IX_KPP_ReelEvents_NeedRecheck' AND object_id = OBJECT_ID(N'dbo.KPP_ReelEvents')
)
BEGIN
    CREATE INDEX IX_KPP_ReelEvents_NeedRecheck
        ON dbo.KPP_ReelEvents (NeedRecheck, NextRecheckAt, RecheckCount)
        INCLUDE (EventKey, SourceTag, EPC, TID, FirstSeen, LastSeen, FinalDirection, ConfidencePct, Task1CId);
END;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes WHERE name = N'IX_KPP_ReelEvents_FirstSeen' AND object_id = OBJECT_ID(N'dbo.KPP_ReelEvents')
)
BEGIN
    CREATE INDEX IX_KPP_ReelEvents_FirstSeen
        ON dbo.KPP_ReelEvents (FirstSeen DESC)
        INCLUDE (EventKey, SourceTag, EPC, TID, Task1CId, FinalDirection, ConfidencePct, NeedRecheck, VideoEventId, SkudExternalId);
END;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes WHERE name = N'IX_KPP_ReelEvents_SourceTag' AND object_id = OBJECT_ID(N'dbo.KPP_ReelEvents')
)
BEGIN
    CREATE INDEX IX_KPP_ReelEvents_SourceTag
        ON dbo.KPP_ReelEvents (SourceTag, FirstSeen DESC)
        INCLUDE (EventKey, FinalDirection, ConfidencePct, Task1CId, Task1CDocIds);
END;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes WHERE name = N'IX_KPP_ReelEvents_Task1CId' AND object_id = OBJECT_ID(N'dbo.KPP_ReelEvents')
)
BEGIN
    CREATE INDEX IX_KPP_ReelEvents_Task1CId
        ON dbo.KPP_ReelEvents (Task1CId, FirstSeen DESC)
        INCLUDE (EventKey, SourceTag, FinalDirection, ConfidencePct, Task1CDocIds);
END;
GO

/*
Рекомендуемые индексы на исходных таблицах, если их еще нет:

CREATE INDEX IX_RFID_Tags_Id_RecordTime ON dbo.RFID_Tags (Id ASC) INCLUDE (RecordTime, Antenna, RSSI, EPC, TID);
CREATE INDEX IX_RFID_Tags_RecordTime ON dbo.RFID_Tags (RecordTime ASC) INCLUDE (Id, Antenna, RSSI, EPC, TID);
CREATE INDEX IX_ReelTransitions_Timestamp ON dbo.ReelTransitions (Timestamp ASC) INCLUDE (Direction, FromCamera, ToCamera, TransportMode);
CREATE INDEX IX_RusGuardLogs_CreatedAt ON dbo.RusGuardLogs (CreatedAt ASC) INCLUDE (Direction, PersonControlDeviceName, FullName, CardNumReal);
*/

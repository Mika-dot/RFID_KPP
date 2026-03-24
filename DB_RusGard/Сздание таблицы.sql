USE [1CTgSend];

-- Проверка наличия таблицы и создание, если её нет
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='RusGuardLogs' AND xtype='U')
BEGIN
    CREATE TABLE [dbo].[RusGuardLogs] (
        -- === Идентификаторы ===
        [ExternalId2]        BIGINT        NOT NULL,
        [PassExternalId2]    BIGINT        NULL,
        [ExternalUserGuid]   NVARCHAR(100) NULL,
        
        -- === Время и устройство ===
        [CreatedAt]          DATETIME2     NOT NULL,
        [PersonControlDeviceName] NVARCHAR(255) NULL,
        
        -- === Направление и статус ===
        [Direction]          NVARCHAR(10)  NOT NULL,
        [ExternalStatus]     NVARCHAR(255) NULL,
        [ExternalStatusId]   INT           NULL,
        
        -- === Информация о человеке/организации ===
        [FullName]           NVARCHAR(255) NULL,
        [FirmName]           NVARCHAR(255) NULL,
        [ExternalPassTypeName] NVARCHAR(100) NULL,
        
        -- === Данные карты/ключа ===
        [KeyName]            NVARCHAR(100) NULL,
        [CardNumParsed]      BIGINT        NULL,
        [CardNumReal]        INT           NULL,
        
        -- === Техническая информация ===
        [ImportedAt]         DATETIME2     DEFAULT (GETDATE()),
        
        -- === Ограничения ===
        CONSTRAINT [PK_RusGuardLogs] PRIMARY KEY CLUSTERED ([ExternalId2]),
        CONSTRAINT [UX_ExternalId2] UNIQUE NONCLUSTERED ([ExternalId2])
    );

    -- Создаем индекс для быстрого поиска по времени
    CREATE NONCLUSTERED INDEX [IX_RusGuardLogs_CreatedAt] 
    ON [dbo].[RusGuardLogs] ([CreatedAt]);
    
    PRINT 'Таблица [dbo].[RusGuardLogs] успешно создана.';
END
ELSE
BEGIN
    PRINT 'Таблица [dbo].[RusGuardLogs] уже существует.';
END
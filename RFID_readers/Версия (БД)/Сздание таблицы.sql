-- ============================================
-- Таблица для хранения данных RFID-меток
-- ============================================
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'[dbo].[RFID_Tags]') AND type in (N'U'))
BEGIN
    CREATE TABLE [dbo].[RFID_Tags] (
        [Id] BIGINT IDENTITY(1,1) PRIMARY KEY,                    -- Уникальный идентификатор записи
        [RecordTime] DATETIME2(3) NOT NULL DEFAULT GETDATE(),     -- Время записи с миллисекундами
        [TagTime] TIME(0) NULL,                                   -- Время из консоли (опционально, для совместимости)
        [Antenna] TINYINT NOT NULL,                               -- Номер антенны (1-4)
        [RSSI] DECIMAL(5,1) NOT NULL,                             -- Уровень сигнала, дБм
        [EPC] VARCHAR(100) NOT NULL,                              -- EPC-код метки (в HEX)
        [TID] VARCHAR(200) NULL,                                  -- TID-код метки (в HEX, опционально)
        [IsProcessed] BIT NOT NULL DEFAULT 0,                     -- Флаг обработки (для 1С/других систем)
        [ProcessedAt] DATETIME2(3) NULL,                          -- Время обработки
        [RawData] NVARCHAR(MAX) NULL                              -- Резерв: сырые данные или комментарии
    );
    
    -- Индексы для ускорения поиска
    CREATE NONCLUSTERED INDEX [IX_RFID_Tags_RecordTime] ON [dbo].[RFID_Tags] ([RecordTime] DESC);
    CREATE NONCLUSTERED INDEX [IX_RFID_Tags_EPC] ON [dbo].[RFID_Tags] ([EPC]);
    CREATE NONCLUSTERED INDEX [IX_RFID_Tags_IsProcessed] ON [dbo].[RFID_Tags] ([IsProcessed]) WHERE [IsProcessed] = 0;
    
    PRINT 'Таблица [dbo].[RFID_Tags] успешно создана.';
END
ELSE
BEGIN
    PRINT 'Таблица [dbo].[RFID_Tags] уже существует.';
END
-- ============================================================
-- Создание таблицы для логов переходов катушек с изображениями
-- Совместимо с MS SQL Server 2016+
-- ============================================================

CREATE TABLE dbo.ReelTransitions (
    -- === PRIMARY KEY ===
    Id BIGINT IDENTITY(1,1) PRIMARY KEY,

    -- === ОБЯЗАТЕЛЬНЫЕ ПОЛЯ ===
    Timestamp DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME(),  -- Время события (точность до мс)
    
    -- === НАПРАВЛЕНИЕ ПЕРЕХОДА ===
    Direction VARCHAR(10) NOT NULL,          -- '0→1' (выехал) или '1→0' (въехал)
    FromCamera TINYINT NOT NULL CHECK (FromCamera IN (0, 1)),  -- Камера источника
    ToCamera TINYINT NOT NULL CHECK (ToCamera IN (0, 1)),      -- Камера назначения
    
    -- === КОНТЕКСТ ПЕРЕНОСА ===
    TransportMode VARCHAR(20) NOT NULL CHECK (TransportMode IN ('alone', 'forklift', 'human', 'forklift+human')),
    
    -- === ТЕХНИЧЕСКИЕ МЕТАДАННЫЕ ===
    TimeDiffSec DECIMAL(5,2) NULL,           -- Задержка между детекциями (сек)
    
    -- === ИЗОБРАЖЕНИЕ (Base64) ===
    ImageBase64 VARCHAR(MAX) NOT NULL,       -- Base64-строка изображения (с камеры 0 или 1)
    ImageSourceCamera TINYINT NOT NULL CHECK (ImageSourceCamera IN (0, 1)),  -- С какой камеры сохранён кадр
    ImageFormat VARCHAR(10) DEFAULT 'jpg',   -- Формат изображения: jpg, png
    
    -- === ОПЦИОНАЛЬНО: ДОП. ИНФОРМАЦИЯ ДЛЯ ОТЛАДКИ ===
    DetectionCount INT DEFAULT 0,            -- Сколько объектов детектировано в кадре
    Notes NVARCHAR(255) NULL                 -- Комментарий/статус
    
    -- === ИНДЕКСЫ ДЛЯ БЫСТРОГО ПОИСКА ===
);

-- Индекс по времени (основной запрос: "показать события за смену/день")
CREATE INDEX IX_ReelTransitions_Timestamp ON dbo.ReelTransitions(Timestamp DESC);

-- Составной индекс для фильтрации по направлению + времени
CREATE INDEX IX_ReelTransitions_Direction_Time ON dbo.ReelTransitions(Direction, Timestamp DESC);

-- Индекс для аналитики: кто чаще возил (по типу транспорта)
CREATE INDEX IX_ReelTransitions_Transport ON dbo.ReelTransitions(TransportMode, Timestamp);

-- ============================================================
-- ПРИМЕРЫ ЗАПРОСОВ (для проверки и использования)
-- ============================================================

-- 🔹 Вставка записи (параметризированный запрос - используйте в коде!)
/*
INSERT INTO dbo.ReelTransitions (
    Timestamp, Direction, FromCamera, ToCamera, 
    TransportMode, TimeDiffSec, 
    ImageBase64, ImageSourceCamera
) VALUES (
    @timestamp, @direction, @fromCam, @toCam,
    @transport, @timeDiff,
    @imageBase64, @sourceCam
);
*/

-- 🔹 Получить последние 50 переходов "на выезде" (0→1)
/*
SELECT TOP 50 
    Timestamp, 
    Direction, 
    TransportMode, 
    TimeDiffSec,
    LEN(ImageBase64) AS ImageSizeBytes,
    ImageSourceCamera
FROM dbo.ReelTransitions 
WHERE Direction = '0→1' 
ORDER BY Timestamp DESC;
*/

-- 🔹 Статистика за сегодня: сколько раз и кто возил
/*
SELECT 
    TransportMode,
    COUNT(*) AS TransitionCount,
    MIN(Timestamp) AS FirstEvent,
    MAX(Timestamp) AS LastEvent
FROM dbo.ReelTransitions 
WHERE CAST(Timestamp AS DATE) = CAST(GETDATE() AS DATE)
GROUP BY TransportMode
ORDER BY TransitionCount DESC;
*/

-- 🔹 Получить Base64-изображение по ID (для отображения в приложении)
/*
SELECT ImageBase64, ImageFormat, Timestamp 
FROM dbo.ReelTransitions 
WHERE Id = @recordId;
*/

-- ============================================================
-- ВАЖНЫЕ ЗАМЕЧАНИЯ ПО ИСПОЛЬЗОВАНИЮ
-- ============================================================